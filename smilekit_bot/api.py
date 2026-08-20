"""Внутренний HTTP-слой для CRM: настройки бота + лаборатория.

Работает в том же asyncio-процессе, что и long polling (см. __main__.py),
на отдельном порту (PORT, Railway его сам прокидывает). Ничего не меняет в
поведении polling-бота — только читает/пишет bot_settings в storage.py и
дёргает тот же SmilekitAssistant, что и реальный бот.

Аутентификация: заголовок x-internal-token сверяется с settings.bot_internal_token
(отдельный секрет от crm_internal_token — тот идёт в обратную сторону, бот -> CRM).
"""

import base64
import binascii
import logging
import time
from pathlib import Path

from aiohttp import web
from aiogram.types import BufferedInputFile

from .config import Settings
from .models import ChatMessage
from .prompts import MANAGER_PROMPT, SUPERVISOR_PROMPT
from .service import SmilekitAssistant
from .storage import BotStorage

DELIVERY_GROUP_SETTING_KEY = "delivery_group_chat_id"
MAX_RECEIPT_BYTES = 20 * 1024 * 1024

LOGGER = logging.getLogger(__name__)
VOICE_ASSETS = {
    "first_use_effect.ogg": Path(__file__).resolve().parent.parent / "assets" / "voice" / "first_use_effect.ogg",
    "not_scam.ogg": Path(__file__).resolve().parent.parent / "assets" / "voice" / "not_scam.ogg",
}
RESULT_ASSETS = {
    "before.jpg": Path(__file__).resolve().parent.parent / "assets" / "results" / "before.jpg",
    "after.jpg": Path(__file__).resolve().parent.parent / "assets" / "results" / "after.jpg",
    # Склеенное фото «до/после» в одном кадре — WhatsApp-канал шлёт им вместо
    # двух отдельных фото (see bot-reply.ts RESULT_PHOTOS).
    "before_after.jpg": Path(__file__).resolve().parent.parent / "assets" / "results" / "before_after.jpg",
    # Тот же набор, что уходит альбомом в Telegram (media.result_photo_group)
    # — WhatsApp-канал берёт эти файлы по ссылке.
    "resultotzyv.jpeg": Path(__file__).resolve().parent.parent / "assets" / "results" / "resultotzyv.jpeg",
    # QR оплаты и инструкция: в Telegram уходят файлами напрямую, WhatsApp-канал
    # берёт их по ссылке (Green API sendFileByUrl).
    "qrcode.jpeg": Path(__file__).resolve().parent.parent / "assets" / "qrcode.jpeg",
    "instructia.jpeg": Path(__file__).resolve().parent.parent / "assets" / "instructia.jpeg",
}
MAX_TRANSCRIPTION_BYTES = 20 * 1024 * 1024

MANAGER_PROMPT_KEY = "manager_prompt"
SUPERVISOR_PROMPT_KEY = "supervisor_prompt"
MODEL_KEY = "model"

# Встроенный MANAGER_PROMPT растёт вместе с бизнес-правилами — лимит должен
# быть заведомо больше дефолта, иначе «сохранить без правок» тихо обрежет
# промпт и живой бот получит урезанную инструкцию.
MAX_PROMPT_LENGTH = 13000


def allowed_models(settings: Settings) -> list[str]:
    """Небольшой фиксированный список моделей, доступных в настройках бота.

    Ничего не выдумываем: единственная модель, для которой заведомо известно,
    что она рабочая в этом окружении — та, что уже настроена в OPENAI_MODEL.
    Список можно расширить через BOT_ALLOWED_MODELS (через запятую), без
    редактирования кода.
    """
    extra = [item.strip() for item in settings.bot_allowed_models.split(",") if item.strip()]
    models = [settings.openai_model, *extra]
    seen: list[str] = []
    for model in models:
        if model not in seen:
            seen.append(model)
    return seen


def _truncate(value: str, limit: int = MAX_PROMPT_LENGTH) -> tuple[str, bool]:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped, False
    return stripped[:limit], True


def effective_settings(storage: BotStorage, settings: Settings) -> dict:
    """Текущие действующие настройки: override из storage либо дефолт из prompts.py."""
    manager_override = storage.get_bot_setting(MANAGER_PROMPT_KEY)
    supervisor_override = storage.get_bot_setting(SUPERVISOR_PROMPT_KEY)
    model_override = storage.get_bot_setting(MODEL_KEY)
    models = allowed_models(settings)
    model = model_override if model_override in models else settings.openai_model
    manager_prompt, manager_was_truncated = _truncate(manager_override or MANAGER_PROMPT)
    supervisor_prompt, supervisor_was_truncated = _truncate(
        supervisor_override or SUPERVISOR_PROMPT
    )
    if manager_was_truncated or supervisor_was_truncated:
        LOGGER.warning(
            "Legacy oversized bot prompts were limited: manager=%s supervisor=%s",
            manager_was_truncated,
            supervisor_was_truncated,
        )
    return {
        "managerPrompt": manager_prompt,
        "managerPromptIsDefault": not bool(manager_override),
        "supervisorPrompt": supervisor_prompt,
        "supervisorPromptIsDefault": not bool(supervisor_override),
        "model": model,
        "models": models,
    }


def _require_auth(request: web.Request) -> web.Response | None:
    settings: Settings = request.app["settings"]
    expected = settings.bot_internal_token.get_secret_value()
    provided = request.headers.get("x-internal-token", "")
    if not expected or provided != expected:
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def get_voice_asset_handler(request: web.Request) -> web.StreamResponse:
    path = VOICE_ASSETS.get(request.match_info["name"])
    if path is None or not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def get_result_asset_handler(request: web.Request) -> web.StreamResponse:
    path = RESULT_ASSETS.get(request.match_info["name"])
    if path is None or not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def post_transcribe_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    encoded = body.get("audioBase64") if isinstance(body, dict) else None
    filename = body.get("filename") if isinstance(body, dict) else None
    if not isinstance(encoded, str) or not isinstance(filename, str):
        return web.json_response({"error": "audioBase64 and filename are required"}, status=400)
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return web.json_response({"error": "invalid_audio"}, status=400)
    if not audio or len(audio) > MAX_TRANSCRIPTION_BYTES:
        return web.json_response({"error": "invalid_audio_size"}, status=400)
    settings: Settings = request.app["settings"]
    assistant: SmilekitAssistant = request.app["assistant"]
    transcript = await assistant.transcribe(audio, filename[:160], settings.transcription_model)
    return web.json_response({"transcript": transcript})


async def get_settings_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    settings: Settings = request.app["settings"]
    return web.json_response(effective_settings(storage, settings))


async def put_settings_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    settings: Settings = request.app["settings"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_body"}, status=400)

    truncated: list[str] = []

    if "managerPrompt" in body:
        value = body["managerPrompt"]
        if not isinstance(value, str):
            return web.json_response({"error": "managerPrompt must be a string"}, status=400)
        text, was_truncated = _truncate(value)
        if was_truncated:
            truncated.append("managerPrompt")
        storage.set_bot_setting(MANAGER_PROMPT_KEY, text)

    if "supervisorPrompt" in body:
        value = body["supervisorPrompt"]
        if not isinstance(value, str):
            return web.json_response({"error": "supervisorPrompt must be a string"}, status=400)
        text, was_truncated = _truncate(value)
        if was_truncated:
            truncated.append("supervisorPrompt")
        storage.set_bot_setting(SUPERVISOR_PROMPT_KEY, text)

    if "model" in body:
        value = body["model"]
        if not isinstance(value, str) or value not in allowed_models(settings):
            return web.json_response({"error": "unknown model"}, status=400)
        storage.set_bot_setting(MODEL_KEY, value)

    # Перечитываем из storage, а не эхо запроса — так CRM видит, что реально
    # записалось (тот же приём, что и в Мостовой saveSettings -> getSettings()).
    result = effective_settings(storage, settings)
    result["truncated"] = truncated
    return web.json_response(result)


async def get_chat_status_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "invalid chat_id"}, status=400)
    return web.json_response({"paused": storage.is_paused(chat_id)})


async def post_chat_pause_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "invalid chat_id"}, status=400)
    storage.cancel_nudge_follow_ups(chat_id)
    storage.pause_chat(chat_id, time.time())
    storage.log_event(chat_id, "info", "operator", "chat.paused", "Чат поставлен на паузу из CRM")
    return web.json_response({"paused": True})


async def post_chat_resume_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "invalid chat_id"}, status=400)
    storage.resume_chat(chat_id)
    storage.log_event(chat_id, "info", "operator", "chat.resumed", "Бот снова отвечает в этом чате")
    return web.json_response({"paused": False})


async def post_chat_message_handler(request: web.Request) -> web.Response:
    """Оператор пишет клиенту вручную из CRM. Отправляет сообщение через
    того же Telegram-бота и автоматически ставит чат на паузу — дальше
    клиента ведёт человек, пока не включат бота обратно (/resume)."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "bot_unavailable"}, status=503)
    storage: BotStorage = request.app["storage"]
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "invalid chat_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "text is required"}, status=400)

    sent = await bot.send_message(chat_id, text)
    storage.cancel_nudge_follow_ups(chat_id)
    storage.pause_chat(chat_id, time.time(), paused_by="operator")
    storage.add_operator_message(chat_id, sent.message_id, text, sender="operator")
    storage.log_event(chat_id, "info", "operator", "chat.manual_message", "Оператор написал клиенту вручную из CRM")
    return web.json_response({"paused": True})


async def post_chat_broadcast_handler(request: web.Request) -> web.Response:
    """Системная рассылка из CRM: отправляет текст, но не ставит чат на паузу."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "bot_unavailable"}, status=503)
    storage: BotStorage = request.app["storage"]
    try:
        chat_id = int(request.match_info["chat_id"])
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid chat_id"}, status=400)
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "text is required"}, status=400)
    value = text.strip()[:4000]
    sent = await bot.send_message(chat_id, value)
    storage.add_operator_message(chat_id, sent.message_id, value, sender="bot")
    storage.log_event(chat_id, "info", "campaign", "campaign.message_sent", "Отправлена рассылка из CRM")
    return web.json_response({"sent": True})


async def get_chats_handler(request: web.Request) -> web.Response:
    """Список чатов для раздела «Диалоги» CRM."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    return web.json_response({"chats": storage.list_chats()})


async def get_chat_messages_handler(request: web.Request) -> web.Response:
    """Переписка одного чата: кто написал (client | bot | operator)."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "invalid chat_id"}, status=400)
    return web.json_response({
        "messages": storage.chat_messages(chat_id),
        "paused": storage.is_paused(chat_id),
    })


async def get_events_handler(request: web.Request) -> web.Response:
    """Живой журнал того, что делает бот — CRM опрашивает раз в несколько
    секунд (see_id — отдаёт только события новее уже полученных)."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    try:
        since_id = int(request.query.get("sinceId", "0"))
    except ValueError:
        since_id = 0
    try:
        limit = int(request.query.get("limit", "150"))
    except ValueError:
        limit = 150
    level = request.query.get("level") or None
    events = storage.list_events(level=level, since_id=since_id, limit=limit)
    return web.json_response({"events": events})


async def post_lab_handler(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    storage: BotStorage = request.app["storage"]
    settings: Settings = request.app["settings"]
    assistant: SmilekitAssistant = request.app["assistant"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return web.json_response({"error": "message is required"}, status=400)

    raw_history = body.get("history") or []
    if not isinstance(raw_history, list):
        return web.json_response({"error": "history must be a list"}, status=400)
    try:
        history = [ChatMessage(role=item["role"], content=item["content"]) for item in raw_history]
    except Exception:
        return web.json_response({"error": "invalid history item"}, status=400)

    current = effective_settings(storage, settings)
    # Изолировано: своя in-memory история из запроса, без storage/crm/telegram —
    # ни одна из persistent-таблиц бота (messages/customer_memory/orders/
    # sent_events) здесь не трогается.
    reply = await assistant.reply(
        body["message"],
        history,
        manager_prompt=current["managerPrompt"],
        supervisor_prompt=current["supervisorPrompt"],
        model=current["model"],
    )
    return web.json_response(
        {
            "reply": reply.text,
            "supervisor": reply.supervisor.model_dump(),
        }
    )


async def post_verify_receipt_handler(request: web.Request) -> web.Response:
    """Проверка чека для внешних каналов (сейчас — WhatsApp/amoCRM), где
    свой приём фото/файлов от Telegram не участвует. Критерии те же самые
    (assistant.verify_receipt), что и для Telegram-бота."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    encoded = body.get("imageBase64") if isinstance(body, dict) else None
    today = body.get("today") if isinstance(body, dict) else None
    kind = body.get("kind") if isinstance(body, dict) else "image"
    mime = body.get("mime") if isinstance(body, dict) else None
    if not isinstance(encoded, str) or not isinstance(today, str):
        return web.json_response({"error": "imageBase64 and today are required"}, status=400)
    if kind not in ("image", "pdf"):
        return web.json_response({"error": "kind must be image or pdf"}, status=400)
    try:
        file_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return web.json_response({"error": "invalid_image"}, status=400)
    if not file_bytes or len(file_bytes) > MAX_RECEIPT_BYTES:
        return web.json_response({"error": "invalid_image_size"}, status=400)
    if not mime:
        mime = "application/pdf" if kind == "pdf" else "image/jpeg"

    assistant: SmilekitAssistant = request.app["assistant"]
    result = await assistant.verify_receipt(f"data:{mime};base64,{encoded}", today, kind=kind)
    return web.json_response(result)


def _delivery_group_chat_id(storage: BotStorage, settings: Settings) -> int:
    saved = storage.get_bot_setting(DELIVERY_GROUP_SETTING_KEY)
    try:
        return int(saved) if saved else settings.delivery_group_chat_id
    except ValueError:
        return settings.delivery_group_chat_id


async def post_push_delivery_handler(request: web.Request) -> web.Response:
    """Отправляет подтверждённый заказ (или проблемный чек на ручную
    проверку) в ту же группу доставки, что уже настроена для Telegram —
    источник заказа виден в подписи (source: 'Telegram' | 'WhatsApp').
    Файл чека передаётся байтами, а не telegram file_id — заказ мог прийти
    не из Telegram, и file_id для него просто не существует."""
    denied = _require_auth(request)
    if denied is not None:
        return denied
    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "bot_unavailable"}, status=503)
    storage: BotStorage = request.app["storage"]
    settings: Settings = request.app["settings"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_body"}, status=400)

    encoded = body.get("imageBase64")
    kind = body.get("kind") or "image"
    if not isinstance(encoded, str):
        return web.json_response({"error": "imageBase64 is required"}, status=400)
    try:
        file_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return web.json_response({"error": "invalid_image"}, status=400)
    if not file_bytes or len(file_bytes) > MAX_RECEIPT_BYTES:
        return web.json_response({"error": "invalid_image_size"}, status=400)

    group_chat_id = _delivery_group_chat_id(storage, settings)
    if not group_chat_id:
        return web.json_response({"error": "delivery_group_not_configured"}, status=409)

    source = str(body.get("source") or "WhatsApp")
    reasons = body.get("mismatchReasons") if isinstance(body.get("mismatchReasons"), list) else None
    if reasons:
        caption = (
            "⚠️ ЧЕК ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ\n\n"
            f"Источник: {source}\n"
            f"Клиент: {body.get('customerName') or '—'}\n"
            f"Телефон: {body.get('customerPhone') or '—'}\n"
            f"Адрес: {body.get('address') or '—'}\n\n"
            "Причины:\n• " + "\n• ".join(str(r) for r in reasons)
        )[:1024]
    else:
        order_summary = str(body.get("orderSummary") or "—")[:350]
        caption = (
            f"Новый оплаченный заказ 📦\n"
            f"Источник: {source}\n"
            f"Имя: {body.get('customerName') or '—'}\n"
            f"Телефон: {body.get('customerPhone') or '—'}\n"
            f"Город: {body.get('city') or '—'}\n"
            f"Адрес: {body.get('address') or '—'}\n"
            f"Что взял: {order_summary}\n"
            f"Сумма: {body.get('amount') or '—'} {body.get('currency') or ''}"
        )

    file_name = "receipt.pdf" if kind == "pdf" else "receipt.jpg"
    input_file = BufferedInputFile(file_bytes, filename=file_name)
    try:
        if kind == "pdf":
            await bot.send_document(group_chat_id, input_file, caption=caption)
        else:
            await bot.send_photo(group_chat_id, input_file, caption=caption)
    except Exception:
        LOGGER.exception("Failed to push %s order to delivery group chat=%s", source, group_chat_id)
        return web.json_response({"error": "send_failed"}, status=502)
    return web.json_response({"ok": True})


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("Unhandled error in %s %s", request.method, request.path)
        return web.json_response({"error": "internal_error"}, status=500)


def create_app(
    settings: Settings, storage: BotStorage, assistant: SmilekitAssistant, bot=None
) -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app["settings"] = settings
    app["storage"] = storage
    app["assistant"] = assistant
    app["bot"] = bot
    app.router.add_get("/internal/health", health)
    app.router.add_get("/media/voice/{name}", get_voice_asset_handler)
    app.router.add_get("/media/results/{name}", get_result_asset_handler)
    app.router.add_get("/internal/settings", get_settings_handler)
    app.router.add_put("/internal/settings", put_settings_handler)
    app.router.add_post("/internal/lab", post_lab_handler)
    app.router.add_post("/internal/transcribe", post_transcribe_handler)
    app.router.add_post("/internal/verify_receipt", post_verify_receipt_handler)
    app.router.add_post("/internal/push_delivery", post_push_delivery_handler)
    app.router.add_get("/internal/chats", get_chats_handler)
    app.router.add_get("/internal/chats/{chat_id}/messages", get_chat_messages_handler)
    app.router.add_get("/internal/chats/{chat_id}/status", get_chat_status_handler)
    app.router.add_post("/internal/chats/{chat_id}/pause", post_chat_pause_handler)
    app.router.add_post("/internal/chats/{chat_id}/resume", post_chat_resume_handler)
    app.router.add_post("/internal/chats/{chat_id}/message", post_chat_message_handler)
    app.router.add_post("/internal/chats/{chat_id}/broadcast", post_chat_broadcast_handler)
    app.router.add_get("/internal/events", get_events_handler)
    return app
