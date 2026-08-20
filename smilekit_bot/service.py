import asyncio
import base64
import io
import json
import logging
from pathlib import Path

import aiohttp
from openai import OpenAI

from .models import AssistantReply, ChatMessage, CrmAction, SupervisorResult
from .privacy import apply_mapping, build_mapping, restore_mapping
from .prompts import DENTAL_ASSISTANT_PROMPT, MANAGER_PROMPT, SUPERVISOR_PROMPT


FALLBACK_TEXT = (
    "Не совсем понял вас. Уточните, пожалуйста, что вы имеете в виду?"
)
LOGGER = logging.getLogger(__name__)
DRAFT_TIMEOUT_SECONDS = 20
REVIEW_TIMEOUT_SECONDS = 15
ROUTE_TIMEOUT_SECONDS = 8
RECEIPT_CHECK_TIMEOUT_SECONDS = 20
DENTAL_TIMEOUT_SECONDS = 25

# Получатель на QR и минимальная сумма — фиксированные факты о текущем
# QR-коде магазина, не про конкретный заказ. Если QR сменится — поменять
# здесь.
PAYMENT_RECIPIENT_NAME = "Халикхан А."
PAYMENT_MIN_AMOUNT_KGS = 1190
PAYMENT_MAX_AMOUNT_KGS = 1490

RECEIPT_CHECK_PROMPT = (
    "Ты проверяешь чек оплаты клиента Smilekit (фото или PDF) по четырём "
    "критериям — оцени каждый отдельно и честно, не угадывай, если не видно:\n"
    "1. is_real_receipt_screenshot — это реальный скриншот банковского "
    "перевода или квитанции об оплате (интерфейс банка/приложения), а не "
    "текстовое сообщение, заметка или случайная картинка.\n"
    f"2. amount_ok — сумма перевода на чеке от {PAYMENT_MIN_AMOUNT_KGS} до "
    f"{PAYMENT_MAX_AMOUNT_KGS} включительно "
    "(в сомах, либо эквивалент в другой валюте, если валюта не сом).\n"
    f"3. recipient_ok — получатель на чеке — «{PAYMENT_RECIPIENT_NAME}» "
    "(совпадение фамилии и инициала; другое имя получателя — это несовпадение).\n"
    "4. date_ok — дата и время перевода на чеке совпадают с сегодняшней датой, "
    "которая будет указана в сообщении ниже (старый или будущий чек — несовпадение).\n"
    "Также обязательно извлеки видимые сумму, получателя и дату в отдельные поля."
)

RECEIPT_TOOL = {
    "type": "function",
    "name": "submit_receipt_check",
    "description": "Сообщить результат проверки чека оплаты по всем критериям.",
    "parameters": {
        "type": "object",
        "properties": {
            "is_real_receipt_screenshot": {
                "type": "boolean",
                "description": "Это реальный скриншот банковского перевода/квитанции, а не текст или случайная картинка.",
            },
            "amount_ok": {
                "type": "boolean",
                "description": f"Сумма на чеке от {PAYMENT_MIN_AMOUNT_KGS} до {PAYMENT_MAX_AMOUNT_KGS} сом включительно.",
            },
            "recipient_ok": {
                "type": "boolean",
                "description": f"Получатель на чеке — «{PAYMENT_RECIPIENT_NAME}».",
            },
            "date_ok": {
                "type": "boolean",
                "description": "Дата и время перевода — сегодняшние.",
            },
            "detected_amount_kgs": {
                "type": ["number", "null"],
                "description": "Сумма перевода, прочитанная на чеке, в сомах; null, если не видна.",
            },
            "detected_recipient": {
                "type": ["string", "null"],
                "description": "Получатель, прочитанный на чеке; null, если не виден.",
            },
            "detected_date": {
                "type": ["string", "null"],
                "description": "Дата перевода, прочитанная на чеке; null, если не видна.",
            },
        },
        "required": [
            "is_real_receipt_screenshot", "amount_ok", "recipient_ok", "date_ok",
            "detected_amount_kgs", "detected_recipient", "detected_date",
        ],
        "additionalProperties": False,
    },
}

# Роутер шаблонов: дешёвый вызов с function calling вместо полного цикла
# черновик+проверка там, где ответ и так один и тот же готовый текст.
# Если сомневается — не вызывает функцию, и reply() идёт по обычному пути.
ROUTE_PROMPT = (
    "Ты — маршрутизатор ответов Smilekit. Если сообщение клиента дословно "
    "попадает под один из готовых сценариев — вызови send_template с его id. "
    "Учитывай историю диалога ниже: не вызывай функцию, если готовый шаблон "
    "уже отправлялся на этот же вопрос или противоречит тому, что клиент "
    "уже говорил. Вызывай, только если уверен; при малейшем сомнении, шутке, "
    "жалобе или составном вопросе с другими темами — не вызывай функцию вообще."
)

ROUTE_TOOL = {
    "type": "function",
    "name": "send_template",
    "description": "Отправить готовый шаблонный ответ вместо генерации нового текста.",
    "parameters": {
        "type": "object",
        "properties": {
            "template_id": {
                "type": "string",
                "enum": [
                    "effect_duration",
                    "first_use_effect",
                    "safety_composition",
                    "single_piece_purchase",
                    "discount_offer",
                    "usage_instructions",
                    "braces_compatibility",
                    "result_doubt",
                    "no_result_refund",
                    "offline_store_question",
                    "delivery_address_question",
                ],
                "description": (
                    "effect_duration — клиент спрашивает, на сколько хватает эффекта, "
                    "сколько он держится или сколько длится результат. "
                    "first_use_effect — клиент спрашивает, какой будет эффект или что "
                    "будет с зубами после применения (не про то, сколько эффект держится). "
                    "safety_composition — клиент спрашивает про состав, безопасность "
                    "состава, вред для эмали или дёсен. "
                    "single_piece_purchase — клиент хочет купить одну полоску или "
                    "меньше целой коробки. "
                    "discount_offer — клиент прямо просит скидку или говорит, что ему "
                    "не хватает денег на полную цену. "
                    "usage_instructions — клиент спрашивает, как пользоваться, как "
                    "наносить полоски или как применять продукт. "
                    "braces_compatibility — клиент спрашивает, можно ли пользоваться "
                    "полосками при брекетах. "
                    "result_doubt — клиент сомневается, сработает ли продукт лично для "
                    "него, не верит в результат или просит доказательства (не про срок "
                    "и не про безопасность). "
                    "no_result_refund — клиент прямо спрашивает, что будет, если "
                    "результата не будет / эффект не появится. "
                    "offline_store_question — клиент спрашивает, есть ли офлайн-магазин "
                    "или шоурум, где можно посмотреть товар лично. "
                    "delivery_address_question — клиент просит адрес магазина, чтобы "
                    "самому забрать или посмотреть товар (не адрес доставки к себе)."
                ),
            }
        },
        "required": ["template_id"],
        "additionalProperties": False,
    },
}

# DeepSeek не поддерживает function calling так же, как OpenAI Responses API —
# просим тот же выбор в виде JSON вместо вызова функции send_template.
ROUTE_JSON_HINT = (
    "Если сообщение подходит под готовый сценарий, верни JSON "
    '{"template_id": "<id>"} с одним из id: effect_duration, '
    "first_use_effect, safety_composition, single_piece_purchase, "
    "discount_offer, usage_instructions, braces_compatibility, result_doubt, "
    "no_result_refund, offline_store_question, delivery_address_question. "
    'Если не уверен или сомневаешься — верни {"template_id": null}. '
    "Всегда отвечай только этим JSON, без markdown."
)

_SUPERVISOR_JSON_HINT = """
Верни ТОЛЬКО валидный JSON (без markdown-обёртки) строго с этими полями:
{
  "status": "approved" | "rewrite",
  "risk_level": "low" | "medium" | "high",
  "issues": ["строка", ...],
  "corrected_response": "строка, пусто если approved",
  "handoff_reason": "",
  "crm_action": {"stage": "строка", "tags": ["строка", ...], "note": "строка"},
  "customer_memory": "строка",
  "order_summary": "строка",
  "expected_client_reply": "строка: к чему относится короткое да/давайте клиента, если он ответит так на ПОСЛЕДНЕЕ сообщение бота — пусто, если бот не делал предложения/вопроса",
  "order": {
    "quantity": число или null,
    "total_amount": число или null,
    "currency": "строка" или null,
    "city": "строка" или null,
    "address": "строка" или null,
    "customer_name": "строка" или null,
    "contact_phone": "строка" или null,
    "contact_username": "строка" или null
  }
}
""".strip()


def _parse_json_loose(content: str) -> dict:
    """DeepSeek иногда оборачивает JSON в ```json ... ``` — снимаем перед
    парсингом; при мусоре вокруг вырезаем внешний объект по фигурным скобкам."""
    text = content.strip()
    fenced = text[:3] == "```"
    if fenced:
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _restore_supervisor_pii(result: SupervisorResult, mapping: dict[str, str]) -> SupervisorResult:
    """Подставляет реальные телефон/адрес/имя обратно в поля супервизора
    после того, как модель отработала на плейсхолдерах вместо PII."""
    if not mapping:
        return result
    result.corrected_response = restore_mapping(result.corrected_response, mapping) or ""
    result.handoff_reason = restore_mapping(result.handoff_reason, mapping) or ""
    result.customer_memory = restore_mapping(result.customer_memory, mapping) or ""
    result.order_summary = restore_mapping(result.order_summary, mapping) or ""
    result.expected_client_reply = restore_mapping(result.expected_client_reply, mapping) or ""
    result.crm_action.note = restore_mapping(result.crm_action.note, mapping) or ""
    result.issues = [restore_mapping(issue, mapping) or issue for issue in result.issues]
    result.order.contact_phone = restore_mapping(result.order.contact_phone, mapping)
    result.order.city = restore_mapping(result.order.city, mapping)
    result.order.address = restore_mapping(result.order.address, mapping)
    result.order.customer_name = restore_mapping(result.order.customer_name, mapping)
    return result


class SmilekitAssistant:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.6-luna",
        knowledge_path: Path | None = None,
        client: OpenAI | None = None,
        deepseek_api_key: str | None = None,
        deepseek_model: str = "deepseek-v4-flash",
        deepseek_base_url: str = "https://api.deepseek.com",
        deepseek_client: OpenAI | None = None,
        gemini_api_key: str | None = None,
        gemini_model: str = "gemini-2.5-flash",
    ) -> None:
        self.model = model
        self.client = client or OpenAI(api_key=api_key)
        path = knowledge_path or Path(__file__).resolve().parent.parent / "knowledge_base.md"
        self.knowledge = path.read_text(encoding="utf-8")
        # Черновик/ревью/роутер — чистый текст, уходят в DeepSeek (дешевле,
        # DeepSeek OpenAI-совместим на /chat/completions). Без ключа DeepSeek
        # выключен, текст идёт на self.client как раньше (deepseek_client
        # остаётся None).
        self.deepseek_model = deepseek_model
        self.deepseek_client = deepseek_client or (
            OpenAI(api_key=deepseek_api_key, base_url=deepseek_base_url) if deepseek_api_key else None
        )
        # Фото чеков (vision) и голосовые (транскрибация) — на Gemini, если
        # ключ задан: умеет оба сценария, дешевле OpenAI. Без ключа оба
        # остаются на self.client (OpenAI), как раньше.
        self.gemini_api_key = gemini_api_key
        self.gemini_model = gemini_model

    async def reply(
        self,
        message: str,
        history: list[ChatMessage],
        *,
        customer_memory: str = "",
        image_data_url: str | None = None,
        manager_prompt: str | None = None,
        supervisor_prompt: str | None = None,
        model: str | None = None,
    ) -> AssistantReply:
        """Собирает ответ менеджера.

        manager_prompt/supervisor_prompt/model — override из настроек CRM
        (см. api.py). None означает «использовать встроенный дефолт» — так
        живой бот и лаборатория используют один и тот же пайплайн.
        """
        effective_manager_prompt = manager_prompt or MANAGER_PROMPT
        effective_supervisor_prompt = supervisor_prompt or SUPERVISOR_PROMPT
        effective_model = model or self.model

        # Телефон/адрес/имя клиента не должны уходить в OpenAI как есть —
        # заменяем на плейсхолдеры перед генерацией и подставляем обратно
        # в готовый ответ ниже (см. reply.text / restore_supervisor_pii).
        pii_mapping = build_mapping(message, customer_memory, *(item.content for item in history))
        redacted_message = apply_mapping(message, pii_mapping)
        redacted_history = [
            ChatMessage(role=item.role, content=apply_mapping(item.content, pii_mapping))
            for item in history
        ]
        redacted_memory = apply_mapping(customer_memory, pii_mapping)

        try:
            draft = await asyncio.wait_for(
                asyncio.to_thread(
                    self._draft,
                    redacted_message,
                    redacted_history,
                    redacted_memory,
                    image_data_url,
                    effective_manager_prompt,
                    effective_model,
                ),
                timeout=DRAFT_TIMEOUT_SECONDS,
            )
        except Exception:
            LOGGER.exception("AI draft generation failed")
            review = SupervisorResult(
                status="rewrite",
                risk_level="high",
                issues=["Внутренняя ошибка генерации"],
                corrected_response=FALLBACK_TEXT,
                handoff_reason="",
                crm_action=CrmAction(
                    stage="Ошибка ответа",
                    tags=["system_error"],
                    note="Черновик модели не был создан",
                ),
            )
            return AssistantReply(text=FALLBACK_TEXT, supervisor=review)

        try:
            review = await asyncio.wait_for(
                asyncio.to_thread(
                    self._review,
                    redacted_message,
                    redacted_history,
                    draft,
                    redacted_memory,
                    image_data_url,
                    effective_supervisor_prompt,
                    effective_model,
                ),
                timeout=REVIEW_TIMEOUT_SECONDS,
            )
        except Exception:
            LOGGER.exception("AI review failed; sending manager draft")
            review = SupervisorResult(
                status="approved",
                risk_level="medium",
                issues=["Проверка гипервизора не завершилась"],
                corrected_response="",
                handoff_reason="",
                crm_action=CrmAction(
                    stage="Диалог",
                    tags=["review_timeout"],
                    note="Клиенту отправлен черновик менеджера",
                ),
            )

        if review.status == "approved":
            text = draft
        elif review.status == "rewrite":
            # Супервизор иногда требует rewrite, но не заполняет
            # corrected_response (пустое поле в structured output) — черновик
            # менеджера обычно нормальный ответ по теме, тогда как FALLBACK_TEXT
            # выглядит как будто бот вообще не понял клиента. Черновик —
            # менее рискованный дефолт, чем готовое признание непонимания.
            text = review.corrected_response.strip() or draft or FALLBACK_TEXT
        text = restore_mapping(text, pii_mapping)
        review = _restore_supervisor_pii(review, pii_mapping)
        return AssistantReply(text=text, supervisor=review)

    async def route(
        self, message: str, history: list[ChatMessage], *, model: str | None = None
    ) -> str | None:
        """template_id, если сообщение точно попадает под готовый шаблон,
        иначе None — тогда reply() идёт по обычному пути. Никогда не бросает
        исключение наружу: сбой роутера не должен ронять обычный ответ."""
        mapping = build_mapping(message, *(item.content for item in history))
        redacted_message = apply_mapping(message, mapping)
        redacted_history = [
            ChatMessage(role=item.role, content=apply_mapping(item.content, mapping))
            for item in history
        ]
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._route, redacted_message, redacted_history, model),
                timeout=ROUTE_TIMEOUT_SECONDS,
            )
        except Exception:
            LOGGER.exception("Template routing failed")
            return None

    def _route(
        self, message: str, history: list[ChatMessage], model: str | None
    ) -> str | None:
        if self.deepseek_client:
            return self._route_deepseek(message, history)
        history_text = "\n".join(
            f"{item.role}: {item.content}" for item in history[-6:]
        ) or "(новый диалог)"
        response = self.client.responses.create(
            model=model or self.model,
            reasoning={"effort": "low"},
            instructions=ROUTE_PROMPT,
            input=[{
                "role": "user",
                "content": f"ИСТОРИЯ:\n{history_text}\n\nСООБЩЕНИЕ КЛИЕНТА:\n{message}",
            }],
            tools=[ROUTE_TOOL],
            tool_choice="auto",
            store=False,
        )
        for item in response.output:
            if getattr(item, "type", None) != "function_call" or item.name != "send_template":
                continue
            try:
                return json.loads(item.arguments).get("template_id")
            except (json.JSONDecodeError, AttributeError):
                return None
        return None

    def _route_deepseek(self, message: str, history: list[ChatMessage]) -> str | None:
        history_text = "\n".join(
            f"{item.role}: {item.content}" for item in history[-6:]
        ) or "(новый диалог)"
        response = self.deepseek_client.chat.completions.create(
            model=self.deepseek_model,
            messages=[
                {"role": "system", "content": f"{ROUTE_PROMPT}\n\n{ROUTE_JSON_HINT}"},
                {
                    "role": "user",
                    "content": f"ИСТОРИЯ:\n{history_text}\n\nСООБЩЕНИЕ КЛИЕНТА:\n{message}",
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=100,
            # V4 по умолчанию включает thinking — на JSON-ответах он иногда
            # съедает весь max_tokens до финального content (см. deepseek.js
            # в Мостовом), поэтому здесь его отключаем.
            extra_body={"thinking": {"type": "disabled"}},
        )
        content = response.choices[0].message.content
        if not content:
            return None
        try:
            return _parse_json_loose(content).get("template_id")
        except (json.JSONDecodeError, AttributeError):
            return None

    _RECEIPT_FAILURE = {
        "is_real_receipt_screenshot": False,
        "amount_ok": False,
        "recipient_ok": False,
        "date_ok": False,
    }

    async def verify_receipt(self, file_data_url: str, today: str, *, kind: str = "image") -> dict:
        """Проверяет чек (фото или PDF) по всем критериям сразу: формат,
        сумма, получатель, дата. today — сегодняшняя дата в Бишкеке,
        'YYYY-MM-DD' (модель не знает реальную дату сама).

        При любом сбое считает чек непроверенным — бот попросит прислать
        чек ещё раз, а не пропускает оплату молча.
        """
        try:
            if self.gemini_api_key:
                return await asyncio.wait_for(
                    self._verify_receipt_gemini(file_data_url, today, kind),
                    timeout=RECEIPT_CHECK_TIMEOUT_SECONDS,
                )
            return await asyncio.wait_for(
                asyncio.to_thread(self._verify_receipt, file_data_url, today, kind),
                timeout=RECEIPT_CHECK_TIMEOUT_SECONDS,
            )
        except Exception:
            LOGGER.exception("Receipt verification failed")
            return dict(self._RECEIPT_FAILURE)

    async def _verify_receipt_gemini(self, file_data_url: str, today: str, kind: str) -> dict:
        header, _, encoded = file_data_url.partition(",")
        mime = header.split(":", 1)[1].split(";", 1)[0] if ":" in header else (
            "application/pdf" if kind == "pdf" else "image/jpeg"
        )
        schema = {
            "type": "OBJECT",
            "properties": {
                "is_real_receipt_screenshot": {"type": "BOOLEAN"},
                "amount_ok": {"type": "BOOLEAN"},
                "recipient_ok": {"type": "BOOLEAN"},
                "date_ok": {"type": "BOOLEAN"},
            },
            "required": ["is_real_receipt_screenshot", "amount_ok", "recipient_ok", "date_ok"],
        }
        parts = [
            {"text": f"{RECEIPT_CHECK_PROMPT}\n\nСегодняшняя дата: {today}."},
            {"inline_data": {"mime_type": mime, "data": encoded}},
        ]
        data = await self._gemini_generate(parts, response_schema=schema)
        result = json.loads(data)
        return {
            "is_real_receipt_screenshot": bool(result.get("is_real_receipt_screenshot")),
            "amount_ok": bool(result.get("amount_ok")),
            "recipient_ok": bool(result.get("recipient_ok")),
            "date_ok": bool(result.get("date_ok")),
        }

    async def _gemini_generate(self, parts: list[dict], *, response_schema: dict | None = None) -> str:
        """Вызывает Gemini generateContent, возвращает текст первого кандидата.
        Бросает исключение при сбое — вызывающий код сам решает, что подставить
        вместо ответа (verify_receipt подставляет отказ, транскрибация — ошибку).
        503 (перегрузка на стороне Google, обычное дело у Gemini) — 2 ретрая
        с паузой, остальные ошибки бросаются сразу."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent"
        payload: dict = {"contents": [{"role": "user", "parts": parts}]}
        if response_schema:
            payload["generationConfig"] = {"responseMimeType": "application/json", "responseSchema": response_schema}
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(1.5 * attempt)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, params={"key": self.gemini_api_key}, json=payload,
                        timeout=aiohttp.ClientTimeout(total=RECEIPT_CHECK_TIMEOUT_SECONDS),
                    ) as response:
                        if response.status == 503:
                            last_error = aiohttp.ClientResponseError(
                                response.request_info, response.history, status=503, message="overloaded"
                            )
                            continue
                        response.raise_for_status()
                        data = await response.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]
            except aiohttp.ClientResponseError as error:
                if error.status != 503:
                    raise
                last_error = error
        raise last_error or RuntimeError("Gemini generateContent failed after retries")

    def _verify_receipt(self, file_data_url: str, today: str, kind: str) -> dict:
        file_content = (
            {"type": "input_image", "image_url": file_data_url, "detail": "high"}
            if kind == "image"
            else {"type": "input_file", "filename": "receipt.pdf", "file_data": file_data_url}
        )
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": "low"},
            instructions=RECEIPT_CHECK_PROMPT,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"Сегодняшняя дата: {today}."},
                    file_content,
                ],
            }],
            tools=[RECEIPT_TOOL],
            tool_choice={"type": "function", "name": "submit_receipt_check"},
            store=False,
        )
        for item in response.output:
            if getattr(item, "type", None) != "function_call" or item.name != "submit_receipt_check":
                continue
            try:
                result = json.loads(item.arguments)
                parsed = {
                    "is_real_receipt_screenshot": bool(result.get("is_real_receipt_screenshot")),
                    "amount_ok": bool(result.get("amount_ok")),
                    "recipient_ok": bool(result.get("recipient_ok")),
                    "date_ok": bool(result.get("date_ok")),
                }
                for key in ("detected_amount_kgs", "detected_recipient", "detected_date"):
                    if result.get(key) is not None:
                        parsed[key] = result[key]
                return parsed
            except (json.JSONDecodeError, AttributeError):
                break
        return dict(self._RECEIPT_FAILURE)

    _GEMINI_AUDIO_MIME = {
        "ogg": "audio/ogg", "oga": "audio/ogg", "mp3": "audio/mp3", "m4a": "audio/mp4",
        "mp4": "audio/mp4", "wav": "audio/wav", "aac": "audio/aac", "flac": "audio/flac",
    }

    async def transcribe(
        self, audio: bytes, filename: str, model: str = "gpt-transcribe"
    ) -> str:
        if self.gemini_api_key:
            return await self._transcribe_gemini(audio, filename)
        return await asyncio.to_thread(self._transcribe, audio, filename, model)

    async def _transcribe_gemini(self, audio: bytes, filename: str) -> str:
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "ogg"
        mime = self._GEMINI_AUDIO_MIME.get(extension, "audio/ogg")
        parts = [
            {"text": (
                "Расшифруй это голосовое сообщение дословно. Ответь ТОЛЬКО текстом "
                "расшифровки, без вводных фраз, кавычек и комментариев. Язык — русский "
                "или кыргызский, определи по речи."
            )},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode("ascii")}},
        ]
        text = (await self._gemini_generate(parts)).strip()
        if not text:
            raise RuntimeError("Gemini returned an empty transcription")
        return text

    def _transcribe(self, audio: bytes, filename: str, model: str) -> str:
        stream = io.BytesIO(audio)
        stream.name = filename
        response = self.client.audio.transcriptions.create(
            model=model,
            file=stream,
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("OpenAI returned an empty transcription")
        return text

    @staticmethod
    def _current_input(message: str, image_data_url: str | None):
        if not image_data_url:
            return {"role": "user", "content": message}
        return {
            "role": "user",
            "content": [
                {"type": "input_text", "text": message},
                {
                    "type": "input_image",
                    "image_url": image_data_url,
                    "detail": "high",
                },
            ],
        }

    def _draft(
        self,
        message: str,
        history: list[ChatMessage],
        customer_memory: str,
        image_data_url: str | None,
        manager_prompt: str = MANAGER_PROMPT,
        model: str | None = None,
    ) -> str:
        # Фото у DeepSeek не поддерживается (нет vision) — с картинкой
        # черновик остаётся на OpenAI, иначе уходит в DeepSeek.
        if self.deepseek_client and not image_data_url:
            return self._draft_deepseek(message, history, customer_memory, manager_prompt)
        memory = customer_memory.strip() or "(память пока пуста)"
        response = self.client.responses.create(
            model=model or self.model,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            instructions=(
                f"{manager_prompt}\n\nБАЗА ЗНАНИЙ:\n{self.knowledge}\n\n"
                f"ПАМЯТЬ О КЛИЕНТЕ:\n{memory}"
            ),
            input=[
                *[item.model_dump() for item in history[-16:]],
                self._current_input(message, image_data_url),
            ],
            store=False,
        )
        text = response.output_text.strip()
        if not text:
            raise RuntimeError("OpenAI returned an empty manager response")
        return text

    def _draft_deepseek(
        self,
        message: str,
        history: list[ChatMessage],
        customer_memory: str,
        manager_prompt: str,
    ) -> str:
        memory = customer_memory.strip() or "(память пока пуста)"
        system = (
            f"{manager_prompt}\n\nБАЗА ЗНАНИЙ:\n{self.knowledge}\n\n"
            f"ПАМЯТЬ О КЛИЕНТЕ:\n{memory}"
        )
        messages = [{"role": "system", "content": system}]
        messages.extend({"role": item.role, "content": item.content} for item in history[-16:])
        messages.append({"role": "user", "content": message})
        response = self.deepseek_client.chat.completions.create(
            model=self.deepseek_model,
            messages=messages,
            temperature=0.35,
            max_tokens=900,
            # См. комментарий в _route_deepseek — V4 thinking включён по
            # умолчанию и на живом трафике уже несколько раз съедал весь
            # max_tokens до финального content (клиент получал заглушку).
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("DeepSeek returned an empty manager response")
        return text

    def _review(
        self,
        message: str,
        history: list[ChatMessage],
        draft: str,
        customer_memory: str,
        image_data_url: str | None,
        supervisor_prompt: str = SUPERVISOR_PROMPT,
        model: str | None = None,
    ) -> SupervisorResult:
        history_text = "\n".join(
            f"{item.role}: {item.content}" for item in history[-16:]
        ) or "(новый диалог)"
        review_text = (
            f"БАЗА ЗНАНИЙ:\n{self.knowledge}\n\n"
            f"ПАМЯТЬ О КЛИЕНТЕ:\n{customer_memory or '(пусто)'}\n\n"
            f"ИСТОРИЯ:\n{history_text}\n\n"
            f"СООБЩЕНИЕ КЛИЕНТА:\n{message}\n\n"
            f"ЧЕРНОВИК:\n{draft}"
        )
        # Фото у DeepSeek не поддерживается (нет vision) — с картинкой ревью
        # остаётся на OpenAI, иначе уходит в DeepSeek.
        if self.deepseek_client and not image_data_url:
            return self._review_deepseek(review_text, supervisor_prompt)
        review_input = self._current_input(review_text, image_data_url)
        response = self.client.responses.parse(
            model=model or self.model,
            reasoning={"effort": "low"},
            instructions=supervisor_prompt,
            input=[review_input],
            text_format=SupervisorResult,
            store=False,
        )
        result = response.output_parsed
        if result is None:
            raise RuntimeError("OpenAI returned no supervisor result")
        return result

    def _review_deepseek(self, review_text: str, supervisor_prompt: str) -> SupervisorResult:
        response = self.deepseek_client.chat.completions.create(
            model=self.deepseek_model,
            messages=[
                {"role": "system", "content": f"{supervisor_prompt}\n\n{_SUPERVISOR_JSON_HINT}"},
                {"role": "user", "content": review_text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=900,
            # См. комментарий в _route_deepseek — thinking съедает max_tokens
            # до финального JSON.
            extra_body={"thinking": {"type": "disabled"}},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("DeepSeek returned no supervisor result")
        data = _parse_json_loose(content)
        return SupervisorResult.model_validate(data)
