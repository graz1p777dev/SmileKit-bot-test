import asyncio
import base64
import logging
import random
import time
from collections import defaultdict
from io import BytesIO
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from aiohttp import web

from .api import create_app, effective_settings
from .config import Settings
from .crm import (
    NON_STANDARD_REQUEST_STAGE,
    REQUEST_TIMEOUT,
    CrmClient,
    build_deal_payload,
    build_order_payload,
    stage_action_for_inbound,
)
from . import templates
from .media import (
    confirms_review_offer,
    result_photo_group,
    review_photo_group,
    should_send_results_after_city,
    wants_result_photos,
    wants_review_photos,
)
from .outgoing import split_outgoing_messages
from .privacy import extract_delivery_details
from .sales import (
    classify_objection,
    confirms_order_offer,
    asks_scam_doubt,
    is_complaint,
    mentions_cooperation_inquiry,
    is_greeting_only,
    mentions_vague_future_promise,
    promised_calendar_date_hours,
    promised_clock_time_hours,
    promised_delay_hours,
    wants_payment_info,
    wants_to_order,
)
from .service import (
    PAYMENT_MAX_AMOUNT_KGS,
    PAYMENT_MIN_AMOUNT_KGS,
    PAYMENT_RECIPIENT_NAME,
    SmilekitAssistant,
)
from .storage import BotStorage


PHOTO_PROMPT = (
    "Клиент прислал фото. Ответь на его подпись и опиши только действительно "
    "видимые детали, относящиеся к улыбке или применению продукта. Не ставь "
    "диагноз и не делай выводов о здоровье зубов по изображению."
)
RATE_LIMIT_TEXT = (
    "Сообщений пришло слишком много за короткое время. "
    "Подождите, пожалуйста, минуту и отправьте вопрос ещё раз."
)
# template_id из assistant.route() -> готовый текст.
ROUTED_TEMPLATES = {
    "effect_duration": templates.EFFECT_DURATION,
    "first_use_effect": templates.FIRST_USE_EFFECT,
    "safety_composition": templates.SAFETY_COMPOSITION,
    "single_piece_purchase": templates.SINGLE_PIECE_POLICY,
    "discount_offer": templates.DISCOUNT_OFFER,
    # Не выдаются роутером напрямую — только вторым/третьим шагом торга,
    # см. эскалацию по storage.claim_event ниже.
    "discount_offer_final": templates.DISCOUNT_OFFER_FINAL,
    "discount_reject": templates.DISCOUNT_REJECT,
    "usage_instructions": templates.USAGE_INSTRUCTIONS,
    "braces_compatibility": templates.BRACES_REPLY,
    "result_doubt": templates.DOUBT_RESULT_REPLY,
    "no_result_refund": templates.NO_RESULT_REFUND_REPLY,
    "offline_store_question": templates.OFFLINE_STORE_COD_REPLY,
    "delivery_address_question": templates.ADDRESS_COD_REPLY,
}
DELIVERY_GROUP_SETTING_KEY = "delivery_group_chat_id"
# Клиент часто пишет мыслями через несколько сообщений подряд (видно на
# живых чатах). Без этого окна каждое такое сообщение уходило в отдельный
# независимый вызов AI, и клиенту прилетало по несколько ответов подряд.
DEBOUNCE_SECONDS = 3.0
# Клиент замолчал после ответа бота — не бросаем диалог: раз бот уже что-то
# показал (эффект, цену), полчаса тишины — разумный порог "перестал отвечать".
IDLE_NUDGE_DELAY_SECONDS = 30 * 60
# Ежедневное напоминание про скидку — время дня якорится на первый контакт
# (написал днём — рассылка днём). Останавливается, как только клиент
# оформил заказ; ограничено, чтобы не превратиться в бесконечный спам.
DAILY_NUDGE_INTERVAL_SECONDS = 24 * 60 * 60
DAILY_NUDGE_MAX_SENDS = 1
DAILY_NUDGE_CHECK_INTERVAL_SECONDS = 15 * 60
# Диалог длиннее этого числа входящих сообщений считается развёрнутой
# консультацией — на второй ступени цепочки простоя другой текст.
CONSULTATION_MESSAGE_THRESHOLD = 3
NUDGE_CHECK_INTERVAL_SECONDS = 15 * 60
QR_CODE_PATH = Path(__file__).resolve().parent.parent / "assets" / "qrcode.jpeg"
INSTRUCTIONS_IMAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "instructia.jpeg"
EFFECT_PHOTO_PATH = Path(__file__).resolve().parent.parent / "assets" / "results" / "after.jpg"
FIRST_USE_EFFECT_VOICE_PATH = Path(__file__).resolve().parent.parent / "assets" / "voice" / "first_use_effect.ogg"
NOT_SCAM_VOICE_PATH = Path(__file__).resolve().parent.parent / "assets" / "voice" / "not_scam.ogg"
DELIVERY_ORDER_VOICE_PATH = Path(__file__).resolve().parent.parent / "assets" / "voice" / "delivery_order.ogg"
# Дожим по оплате: QR не шлём повторно, только переспрашиваем; после
# нескольких напоминаний останавливаемся — менеджер разберётся вручную,
# когда клиент всё же пришлёт чек.
AWAITING_PAYMENT_REMINDER_DELAY_SECONDS = 2 * 60 * 60
AWAITING_PAYMENT_MAX_REMINDERS = 3
# Ночью (Бишкек, UTC+6) рассылку про оплату не шлём — переносим на утро.
QUIET_HOURS_START_BISHKEK = 22
QUIET_HOURS_END_BISHKEK = 9
BISHKEK_UTC_OFFSET_SECONDS = 6 * 3600
# Первый дожим простоя: написал до 19:00 — дожим в 19:30 в тот же день;
# написал после 19:00 — в 12:00 на следующий день.
FOLLOWUP_CUTOFF_HOUR_BISHKEK = 19
FOLLOWUP_SAME_DAY_SEND_HOUR_BISHKEK = 19.5
FOLLOWUP_NEXT_DAY_SEND_HOUR_BISHKEK = 12


class GroupSilenceMiddleware(BaseMiddleware):
    """В группах бот служит только доставке и не ведёт клиентский диалог."""

    async def __call__(self, handler, event: Message, data):
        if event.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            command = (event.text or "").split(maxsplit=1)[0].lower()
            if not command.startswith("/delivery_group"):
                return None
        return await handler(event, data)


def next_idle_followup_due_at(now: float) -> float:
    shifted = now + BISHKEK_UTC_OFFSET_SECONDS
    day_start = shifted - (shifted % 86400)
    hour = (shifted - day_start) / 3600
    if hour < FOLLOWUP_CUTOFF_HOUR_BISHKEK:
        target = day_start + FOLLOWUP_SAME_DAY_SEND_HOUR_BISHKEK * 3600
    else:
        target = day_start + 86400 + FOLLOWUP_NEXT_DAY_SEND_HOUR_BISHKEK * 3600
    return target - BISHKEK_UTC_OFFSET_SECONDS


def next_allowed_send_time(now: float) -> float | None:
    """None, если сейчас можно слать; иначе — момент (09:00 по Бишкеку),
    когда тихие часы закончатся."""
    shifted = now + BISHKEK_UTC_OFFSET_SECONDS
    hour = int(shifted // 3600) % 24
    if not (hour >= QUIET_HOURS_START_BISHKEK or hour < QUIET_HOURS_END_BISHKEK):
        return None
    day_start = shifted - (shifted % 86400)
    target = day_start + QUIET_HOURS_END_BISHKEK * 3600
    if target <= shifted:
        target += 86400
    return target - BISHKEK_UTC_OFFSET_SECONDS


async def main() -> None:
    settings = Settings()
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    dp = Dispatcher()
    dp.message.middleware(GroupSilenceMiddleware())
    assistant = SmilekitAssistant(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.openai_model,
        deepseek_api_key=settings.deepseek_api_key.get_secret_value() or None,
        deepseek_model=settings.deepseek_model,
        deepseek_base_url=settings.deepseek_base_url,
        gemini_api_key=settings.gemini_api_key.get_secret_value() or None,
        gemini_model=settings.gemini_model,
    )
    storage = BotStorage(settings.database_path)
    crm = CrmClient(
        settings.crm_base_url,
        settings.crm_internal_token.get_secret_value(),
    )
    if not crm.enabled:
        logging.info("CRM order push disabled: CRM_BASE_URL/CRM_INTERNAL_TOKEN not set")
    locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    follow_up_tasks: set[asyncio.Task] = set()
    crm_tasks: set[asyncio.Task] = set()
    pending_texts: dict[int, list[str]] = defaultdict(list)
    debounce_tasks: dict[int, asyncio.Task] = {}

    async def follow_up_after_results(chat_id: int, message_id: int) -> None:
        await asyncio.sleep(3 * 60)
        if storage.is_paused(chat_id):
            return  # диалог ведёт менеджер — бот молчит полностью
        if storage.has_message_after(chat_id, message_id):
            return
        if not storage.claim_event(chat_id, "results_follow_up"):
            return
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(random.uniform(0.8, 1.3))
        text = "Если хотите, могу отправить ещё отзывы наших клиентов 😊"
        sent = await bot.send_message(chat_id, text)
        storage.add_operator_message(chat_id, sent.message_id, text)
        storage.log_event(chat_id, "info", "nudge", "nudge.results_follow_up", "Дожим после результатов отправлен")

    async def idle_nudge(chat_id: int, message_id: int) -> None:
        """Клиент замолчал после ответа бота — напоминаем об эффекте один раз
        на этот простой. claim_event по message_id, а не по chat_id: после
        следующего сообщения клиента новый простой сможет напомнить снова."""
        await asyncio.sleep(IDLE_NUDGE_DELAY_SECONDS)
        if storage.is_paused(chat_id):
            return  # диалог ведёт менеджер — бот молчит полностью
        if storage.has_message_after(chat_id, message_id):
            return
        if storage.get_pending_payment(chat_id) is not None:
            # Ждём чек по уже отправленному QR — рассказывать про эффект
            # посреди оплаты неуместно, дожимом занимается awaiting_payment.
            return
        if not storage.claim_event(chat_id, f"idle_nudge:{message_id}"):
            return
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(random.uniform(0.8, 1.3))
        if EFFECT_PHOTO_PATH.exists():
            sent = await bot.send_photo(chat_id, FSInputFile(EFFECT_PHOTO_PATH), caption=templates.FIRST_USE_EFFECT)
        else:
            sent = await bot.send_message(chat_id, templates.FIRST_USE_EFFECT)
        storage.add_operator_message(chat_id, sent.message_id, templates.FIRST_USE_EFFECT)
        storage.log_event(chat_id, "info", "nudge", "nudge.idle", "Напоминание об эффекте после простоя отправлено")

    async def check_daily_nudges() -> None:
        while True:
            await asyncio.sleep(DAILY_NUDGE_CHECK_INTERVAL_SECONDS)
            now = time.time()
            for chat_id in storage.due_daily_nudges(now, DAILY_NUDGE_MAX_SENDS):
                if storage.is_paused(chat_id):
                    # Диалог ведёт менеджер — пропускаем молча, слот считаем
                    # использованным, чтобы после resume не сыпались старые.
                    storage.advance_daily_nudge(chat_id, now + DAILY_NUDGE_INTERVAL_SECONDS)
                    continue
                try:
                    currency = templates.currency_for_history(
                        [item.content for item in storage.history(chat_id)]
                    )
                    await bot.send_chat_action(chat_id, ChatAction.TYPING)
                    await asyncio.sleep(random.uniform(0.6, 1.0))
                    daily_text = templates.daily_discount_offer(currency)
                    sent = await bot.send_message(chat_id, daily_text)
                    storage.add_operator_message(chat_id, sent.message_id, daily_text)
                    storage.log_event(chat_id, "info", "nudge", "nudge.daily_discount", "Ежедневное напоминание про скидку отправлено")
                except Exception:
                    logging.exception("Daily nudge send failed chat=%s", chat_id)
                    storage.log_event(chat_id, "error", "nudge", "nudge.daily_discount_failed", "Не удалось отправить ежедневное напоминание")
                storage.advance_daily_nudge(chat_id, now + DAILY_NUDGE_INTERVAL_SECONDS)

    async def check_nudge_follow_ups() -> None:
        """Простой без ответа / незавершённый заказ — цепочка напоминаний из
        storage.schedule_nudge_chain/schedule_order_incomplete_nudge."""
        while True:
            await asyncio.sleep(NUDGE_CHECK_INTERVAL_SECONDS)
            now = time.time()
            for _id, chat_id, kind in storage.due_nudge_follow_ups(now):
                if storage.is_paused(chat_id):
                    continue  # диалог ведёт менеджер — напоминание сгорает молча
                try:
                    if kind == "awaiting_payment":
                        pending = storage.get_pending_payment(chat_id)
                        if pending is None:
                            continue  # уже оплатил или отменили — дожимать нечего
                        resume_at = next_allowed_send_time(now)
                        if resume_at is not None:
                            # Ночь по Бишкеку — не пишем, переносим на утро,
                            # счётчик попыток не тратим.
                            storage.schedule_awaiting_payment_reminder(chat_id, resume_at)
                            continue
                        count = storage.increment_payment_reminder(chat_id)
                        await bot.send_chat_action(chat_id, ChatAction.TYPING)
                        await asyncio.sleep(random.uniform(0.6, 1.0))
                        variant = templates.AWAITING_PAYMENT_REMINDERS[(count - 1) % len(templates.AWAITING_PAYMENT_REMINDERS)]
                        sent = await bot.send_message(chat_id, variant)
                        storage.add_operator_message(chat_id, sent.message_id, variant)
                        storage.log_event(chat_id, "info", "nudge", "nudge.awaiting_payment", f"Напоминание про оплату отправлено ({count}/{AWAITING_PAYMENT_MAX_REMINDERS})")
                        if count < AWAITING_PAYMENT_MAX_REMINDERS:
                            storage.schedule_awaiting_payment_reminder(
                                chat_id, now + AWAITING_PAYMENT_REMINDER_DELAY_SECONDS
                            )
                        continue
                    if kind == "day" and storage.incoming_message_count(chat_id) >= CONSULTATION_MESSAGE_THRESHOLD:
                        template = templates.AFTER_CONSULT_FOLLOWUP
                    else:
                        template = {
                            "hours": templates.IDLE_FOLLOWUP,
                            "day": templates.DISCOUNT_FOLLOWUP,
                            "last": templates.LAST_MESSAGE_FOLLOWUP,
                            "order_incomplete": templates.ORDER_INCOMPLETE_FOLLOWUP,
                            "promised": templates.PROMISED_FOLLOWUP,
                        }[kind]
                    author = await bot.get_chat(chat_id)
                    text = templates.fill_nudge_template(template, author.first_name)
                    await bot.send_chat_action(chat_id, ChatAction.TYPING)
                    await asyncio.sleep(random.uniform(0.6, 1.0))
                    sent = await bot.send_message(chat_id, text)
                    storage.add_operator_message(chat_id, sent.message_id, text)
                    storage.log_event(chat_id, "info", "nudge", f"nudge.{kind}", f"Дожим простоя «{kind}» отправлен")
                except Exception:
                    logging.exception("Nudge follow-up send failed chat=%s kind=%s", chat_id, kind)
                    storage.log_event(chat_id, "error", "nudge", "nudge.failed", f"Не удалось отправить дожим «{kind}»")

    async def _submit_order_and_advance(chat_id: int, payload: dict) -> None:
        delivered = await crm.send_order(payload)
        if delivered:
            # Заказ оформлен — сделка передаётся менеджеру на закрытие.
            await crm.advance_stage(chat_id, "order")

    async def send_delivery_order_request(chat_id: int) -> Message:
        """Просит данные для доставки голосом; текст остаётся запасным вариантом."""
        if DELIVERY_ORDER_VOICE_PATH.exists():
            await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
            await asyncio.sleep(random.uniform(0.5, 0.9))
            return await bot.send_voice(chat_id, FSInputFile(DELIVERY_ORDER_VOICE_PATH))
        logging.warning("Delivery order voice missing at %s", DELIVERY_ORDER_VOICE_PATH)
        return await bot.send_message(chat_id, templates.PAYMENT_QR_MESSAGE)

    async def push_order(message: Message, supervisor) -> bool:
        """Запрашивает оплату по QR и заводит заказ в CRM.

        Инструкцию и благодарность бот присылает не сразу, а после того как
        клиент пришлёт чек и он пройдёт проверку (см. handle_photo).
        Ответ клиенту уже отправлен и ждать сеть не должен, поэтому CRM-задача
        не ожидается здесь: результат и ошибки уходят в лог внутри send_order.
        Клиентские сообщения claim_event-дедуплицированы по составу заказа —
        повторное подтверждение того же состава (тот же externalKey) их не
        повторит, а изменившийся состав — новый заказ, новый запрос оплаты.
        """
        author = message.from_user
        payload = build_order_payload(
            message.chat.id,
            supervisor,
            customer_name=author.full_name if author else None,
            telegram_username=author.username if author else None,
        )
        if payload is None:
            return False
        chat_id = message.chat.id
        storage.cancel_daily_nudge(chat_id)
        storage.cancel_nudge_follow_ups(chat_id)
        if storage.claim_event(chat_id, f"order_thanks:{payload['externalKey']}"):
            # ИИ не всегда фиксирует итоговую сумму при индивидуальной
            # скидке — если её нет, но скидка предлагалась в этом диалоге,
            # это надёжнее как дефолт, чем полная цена.
            # Скидка двухступенчатая: 13% (1290), затем 20% (1190).
            if storage.has_event(chat_id, "discount_offered_final"):
                default_amount = 1190
            elif storage.has_event(chat_id, "discount_offered"):
                default_amount = 1290
            else:
                default_amount = 1490
            amount = payload.get("totalAmount") or (default_amount * (payload.get("quantity") or 1))
            currency = payload.get("currency") or "KGS"
            storage.save_pending_payment(
                chat_id,
                order_summary=payload.get("summary") or "",
                amount=amount,
                currency=currency,
                customer_name=payload.get("customerName"),
                customer_phone=payload.get("customerPhone"),
                city=payload.get("city"),
                address=payload.get("address"),
                customer_username=payload.get("customerUsername"),
                source="Telegram",
            )
            if QR_CODE_PATH.exists():
                await bot.send_photo(chat_id, FSInputFile(QR_CODE_PATH))
            else:
                logging.warning("QR code file missing at %s — assets/qrcode.jpeg not provided", QR_CODE_PATH)
            request_message = await send_delivery_order_request(chat_id)
            storage.add_operator_message(
                chat_id, request_message.message_id,
                "[голосовое: отправьте чек об оплате, адрес доставки и номер телефона]",
            )
            storage.schedule_awaiting_payment_reminder(
                chat_id, time.time() + AWAITING_PAYMENT_REMINDER_DELAY_SECONDS
            )
            storage.log_event(
                chat_id, "info", "order", "order.confirmed",
                "Заказ подтверждён через ИИ-диалог, QR отправлен",
                {"amount": amount, "currency": currency, "city": payload.get("city")},
            )
        if not crm.enabled:
            return True
        task = asyncio.create_task(_submit_order_and_advance(message.chat.id, payload))
        crm_tasks.add(task)
        task.add_done_callback(crm_tasks.discard)
        return True

    async def push_to_delivery_group(payment: dict, receipt_file_id: str, receipt_kind: str = "image") -> bool:
        """Пересылает оплаченный заказ доставщикам вместе с чеком клиента.

        Группа регистрируется администратором командой /delivery_group.
        Без неё заказ остаётся в журнале и не отправляется в случайный чат.
        """
        order_summary = str(payment.get("order_summary") or "—")[:350]
        caption = (
            f"Новый оплаченный заказ 📦\n"
            f"Источник: {payment.get('source') or 'Telegram'}\n"
            f"Имя: {payment.get('customer_name') or '—'}\n"
            f"Ник: {('@' + str(payment.get('customer_username')).lstrip('@')) if payment.get('customer_username') else '—'}\n"
            f"Телефон: {payment.get('customer_phone') or '—'}\n"
            f"Город: {payment.get('city') or '—'}\n"
            f"Адрес: {payment.get('address') or '—'}\n"
            f"Что взял: {order_summary}\n"
            f"Сумма: {payment.get('amount')} {payment.get('currency') or ''}"
        )
        saved_group_id = storage.get_bot_setting(DELIVERY_GROUP_SETTING_KEY)
        try:
            group_chat_id = int(saved_group_id) if saved_group_id else settings.delivery_group_chat_id
        except ValueError:
            group_chat_id = settings.delivery_group_chat_id
        if not group_chat_id:
            logging.warning(
                "Delivery group not configured yet (DELIVERY_GROUP_CHAT_ID=0). Order details:\n%s",
                caption,
            )
            return False
        try:
            if receipt_kind == "pdf":
                await bot.send_document(group_chat_id, receipt_file_id, caption=caption)
            else:
                await bot.send_photo(group_chat_id, receipt_file_id, caption=caption)
            storage.log_event(
                payment.get("chat_id"), "info", "order", "order.sent_to_delivery_group",
                "Оплаченный заказ и чек отправлены в группу доставки",
                {"deliveryGroupChatId": group_chat_id},
            )
            return True
        except Exception:
            logging.exception("Failed to send paid order to delivery group chat=%s", group_chat_id)
            storage.log_event(
                payment.get("chat_id"), "error", "order", "order.delivery_group_failed",
                "Не удалось отправить оплаченный заказ в группу доставки",
                {"deliveryGroupChatId": group_chat_id},
            )
            return False

    def receipt_mismatch_reasons(result: dict, today: str) -> list[str]:
        if not result.get("is_real_receipt_screenshot"):
            return ["изображение не подтверждено как банковский чек или квитанция"]
        reasons = []
        if not result.get("amount_ok"):
            detected = result.get("detected_amount_kgs")
            amount = f"распознано {detected:g} сом; " if isinstance(detected, (int, float)) else ""
            reasons.append(
                f"сумма не совпадает: {amount}допустимо {PAYMENT_MIN_AMOUNT_KGS}–{PAYMENT_MAX_AMOUNT_KGS} сом"
            )
        if not result.get("recipient_ok"):
            detected = str(result.get("detected_recipient") or "не распознан")
            reasons.append(
                f"получатель не совпадает: распознано «{detected}», требуется «{PAYMENT_RECIPIENT_NAME}»"
            )
        if not result.get("date_ok"):
            detected = str(result.get("detected_date") or "не распознана")
            reasons.append(
                f"дата не совпадает: распознано «{detected}», требуется сегодняшняя дата {today}"
            )
        return reasons or ["чек не прошёл обязательные проверки"]

    async def push_receipt_mismatch_to_delivery_group(
        payment: dict, receipt_file_id: str, receipt_kind: str, result: dict, today: str
    ) -> bool:
        raw_chat_id = storage.get_bot_setting(DELIVERY_GROUP_SETTING_KEY)
        if not raw_chat_id:
            storage.log_event(
                payment.get("chat_id"), "error", "order", "order.receipt_review_group_missing",
                "Проблемный чек не отправлен: группа доставки не настроена",
            )
            return False
        reasons = receipt_mismatch_reasons(result, today)
        caption = (
            "⚠️ ЧЕК ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ\n\n"
            f"Клиент: {payment.get('customer_name') or '—'}\n"
            f"Ник: {('@' + str(payment.get('customer_username')).lstrip('@')) if payment.get('customer_username') else '—'}\n"
            f"Телефон: {payment.get('customer_phone') or '—'}\n"
            f"Адрес: {payment.get('address') or '—'}\n\n"
            "Причины:\n• " + "\n• ".join(reasons)
        )[:1024]
        try:
            group_chat_id = int(raw_chat_id)
            if receipt_kind == "pdf":
                await bot.send_document(group_chat_id, receipt_file_id, caption=caption)
            else:
                await bot.send_photo(group_chat_id, receipt_file_id, caption=caption)
            storage.log_event(
                payment.get("chat_id"), "warn", "order", "order.receipt_sent_for_review",
                "Проблемный чек отправлен в группу доставки", {"reasons": reasons},
            )
            return True
        except Exception as exc:
            logging.exception("Failed to send mismatched receipt to delivery group")
            storage.log_event(
                payment.get("chat_id"), "error", "order", "order.receipt_review_send_failed",
                str(exc), {"reasons": reasons},
            )
            return False

    def missing_delivery_details(payment: dict) -> list[str]:
        missing = []
        if not str(payment.get("city") or "").strip():
            missing.append("город")
        if not str(payment.get("address") or "").strip():
            missing.append("адрес доставки")
        if not str(payment.get("customer_phone") or "").strip():
            missing.append("номер телефона")
        return missing

    async def complete_verified_order(message: Message, payment: dict) -> None:
        receipt_file_id = str(payment.get("receipt_file_id") or "")
        if not receipt_file_id or missing_delivery_details(payment):
            return
        chat_id = message.chat.id
        storage.clear_pending_payment(chat_id)
        storage.cancel_nudge_follow_ups(chat_id)
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(random.uniform(0.6, 1.0))
        await push_to_delivery_group(
            payment,
            receipt_file_id,
            str(payment.get("receipt_kind") or "image"),
        )
        receipt_url = await telegram_file_url(receipt_file_id)
        if receipt_url:
            await crm.attach_receipt(chat_id, receipt_url)
        await message.answer(templates.THANK_YOU_ORDER)
        storage.log_event(
            chat_id, "info", "order", "order.completed",
            "Чек, адрес и телефон получены — заказ передан в доставку",
        )

    def push_deal(message: Message, *, is_new_conversation: bool) -> None:
        """Заводит сделку в CRM при первом сообщении клиента этому боту."""
        if not crm.enabled or not is_new_conversation:
            return
        author = message.from_user
        payload = build_deal_payload(
            message.chat.id,
            customer_name=author.full_name if author else None,
            telegram_username=author.username if author else None,
        )
        task = asyncio.create_task(crm.create_deal(payload))
        crm_tasks.add(task)
        task.add_done_callback(crm_tasks.discard)

    def push_stage(message: Message, text: str) -> None:
        """Продвигает сделку по воронке по распознанному намерению клиента."""
        if not crm.enabled:
            return
        action = stage_action_for_inbound(text)
        if not action:
            return
        task = asyncio.create_task(crm.advance_stage(message.chat.id, action))
        crm_tasks.add(task)
        task.add_done_callback(crm_tasks.discard)

    async def claim(message: Message, *, rate_limit: bool = True) -> bool:
        chat_id = message.chat.id
        if not storage.claim_message(chat_id, message.message_id):
            logging.info(
                "Ignoring duplicate Telegram message chat=%s message=%s",
                chat_id,
                message.message_id,
            )
            return False
        if rate_limit and not storage.allow_request(
            chat_id, settings.rate_limit_per_minute
        ):
            await message.answer(RATE_LIMIT_TEXT)
            return False
        return True

    async def telegram_file_url(file_id: str) -> str | None:
        """Публичная ссылка на файл Telegram — тем же токеном, что download().
        Не требует скачивания байтов: CRM просто хранит ссылку рядом с
        заказом (см. attach_receipt), сам файл остаётся на серверах Telegram."""
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            return None
        return f"https://api.telegram.org/file/bot{settings.telegram_bot_token.get_secret_value()}/{telegram_file.file_path}"

    async def download(file_id: str) -> bytes:
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            raise RuntimeError("Telegram returned no file path")
        output = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=output)
        return output.getvalue()

    def schedule_idle_follow_up(chat_id: int, promised_hours: float | None) -> None:
        """QR уже отправлен и чек ещё не пришёл — дожим по оплате важнее
        обычной цепочки простоя. Иначе — клиент сам назвал момент возврата
        («завтра зп — закажу»): напоминание ставится точно на этот момент."""
        if storage.get_pending_payment(chat_id) is not None:
            storage.schedule_awaiting_payment_reminder(
                chat_id, time.time() + AWAITING_PAYMENT_REMINDER_DELAY_SECONDS
            )
        elif promised_hours is not None:
            storage.schedule_promised_follow_up(chat_id, time.time() + promised_hours * 3600)
        else:
            now = time.time()
            storage.schedule_nudge_chain(chat_id, now, next_idle_followup_due_at(now))

    async def process(
        message: Message,
        text: str,
        *,
        kind: str,
        image_data_url: str | None = None,
    ) -> None:
        chat_id = message.chat.id
        logging.info(
            "Processing Telegram message chat=%s message=%s kind=%s",
            chat_id,
            message.message_id,
            kind,
        )
        if storage.is_paused(chat_id):
            # Оператор ведёт этот чат вручную из CRM — бот только сохраняет
            # сообщение клиента и молчит, пока чат не включат обратно.
            storage.add_incoming_message(
                chat_id, message.message_id,
                f"[{kind}] {text}" if kind != "text" else text,
                kind,
            )
            logging.info("Chat paused, skipping auto-reply chat=%s message=%s", chat_id, message.message_id)
            storage.log_event(chat_id, "info", "message", "message.paused_skip", "Чат на паузе — сообщение сохранено, бот молчит")
            return

        history = storage.history(chat_id)
        is_new_conversation = not history
        # Клиент написал сам — прежние напоминания о простое устарели.
        storage.cancel_nudge_follow_ups(chat_id)
        push_deal(message, is_new_conversation=is_new_conversation)
        if kind == "text":
            push_stage(message, text)

        # Клиент сам назвал момент, когда вернётся («завтра зп — закажу»,
        # «напишу к 18:00») — напоминание ставим точно на этот момент, а не
        # обычную цепочку. Точное время (если названо) приоритетнее дня.
        promised_hours = None
        if kind == "text":
            promised_hours = promised_clock_time_hours(text, time.time())
            if promised_hours is None:
                promised_hours = promised_delay_hours(text)
            if promised_hours is None:
                promised_hours = promised_calendar_date_hours(text, time.time())

        # Клиент откладывает заказ без определимой даты («через пару дней»,
        # «после зарплаты») — вместо того чтобы гадать интервал, уточняем
        # число и не дожимаем раньше срока, который клиент сам не назвал.
        if (
            kind == "text"
            and not is_new_conversation
            and promised_hours is None
            and mentions_vague_future_promise(text)
        ):
            await message.answer(templates.CLARIFY_VAGUE_DATE_REPLY)
            storage.add_exchange(chat_id, message.message_id, text, templates.CLARIFY_VAGUE_DATE_REPLY, kind)
            logging.info("Vague future promise, asked to clarify chat=%s message=%s", chat_id, message.message_id)
            storage.log_event(chat_id, "info", "message", "message.vague_promise", "Клиент отложил заказ без даты — уточняем", {"text": text})
            schedule_idle_follow_up(chat_id, None)
            return

        # Сомнение в честности («вы не мошенники?») — раньше ветки жалоб,
        # т.к. слова пересекаются. Отвечаем готовым голосовым от владельца.
        if kind == "text" and asks_scam_doubt(text) and NOT_SCAM_VOICE_PATH.exists():
            await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
            await asyncio.sleep(random.uniform(0.9, 1.5))
            await bot.send_voice(chat_id, FSInputFile(NOT_SCAM_VOICE_PATH))
            stored = "[голосовое: мы не мошенники, нам можно доверять]"
            storage.add_exchange(chat_id, message.message_id, text, stored, kind)
            logging.info("Scam doubt answered with voice chat=%s message=%s", chat_id, message.message_id)
            storage.log_event(chat_id, "info", "reply", "reply.scam_doubt_voice", "Сомнение в честности — отправлено голосовое", {"text": text})
            schedule_idle_follow_up(chat_id, promised_hours)
            return

        # Повторное приветствие старого клиента не перезапускает первую
        # воронку и не повторяет цену/город — просто продолжаем диалог.
        if kind == "text" and not is_new_conversation and is_greeting_only(text):
            normalized_greeting = text.lower()
            variants = (
                templates.RETURNING_KYRGYZ_GREETING_REPLIES
                if "салам" in normalized_greeting
                else templates.RETURNING_GREETING_REPLIES
            )
            reply_text = random.choice(variants)
            await message.answer(reply_text)
            storage.add_exchange(chat_id, message.message_id, text, reply_text, kind)
            schedule_idle_follow_up(chat_id, promised_hours)
            return

        # Явное недовольство — не додумываем причину и не извиняемся вслепую:
        # прямо спрашиваем, что случилось, шлём важное уведомление в CRM и
        # переносим сделку в «Нестандартный запрос» — разбирает человек.
        if kind == "text" and not is_new_conversation and is_complaint(text):
            await message.answer(templates.COMPLAINT_PROMPT)
            storage.add_exchange(chat_id, message.message_id, text, templates.COMPLAINT_PROMPT, kind)
            task = asyncio.create_task(crm.notify_important(
                title="Жалоба клиента (Smilekit)",
                body=f"Сообщение клиента: {text}",
                chat_id=chat_id,
            ))
            crm_tasks.add(task)
            task.add_done_callback(crm_tasks.discard)
            stage_task = asyncio.create_task(crm.advance_stage(chat_id, NON_STANDARD_REQUEST_STAGE))
            crm_tasks.add(stage_task)
            stage_task.add_done_callback(crm_tasks.discard)
            logging.info("Complaint detected chat=%s message=%s", chat_id, message.message_id)
            storage.log_event(chat_id, "warn", "message", "message.complaint", "Обнаружена жалоба — сделка перенесена в «Нестандартный запрос»", {"text": text})
            schedule_idle_follow_up(chat_id, promised_hours)
            return

        # Опт/сотрудничество/реклама — не розничный сценарий, не продаём по
        # обычному скрипту. Отвечаем коротко и переносим сделку в этап
        # «Нестандартный запрос» на ручной разбор.
        if kind == "text" and not is_new_conversation and mentions_cooperation_inquiry(text):
            await message.answer(templates.COOPERATION_INQUIRY_REPLY)
            storage.add_exchange(chat_id, message.message_id, text, templates.COOPERATION_INQUIRY_REPLY, kind)
            task = asyncio.create_task(crm.notify_important(
                title="Запрос о сотрудничестве (Smilekit)",
                body=f"Сообщение клиента: {text}",
                chat_id=chat_id,
            ))
            crm_tasks.add(task)
            task.add_done_callback(crm_tasks.discard)
            stage_task = asyncio.create_task(crm.advance_stage(chat_id, NON_STANDARD_REQUEST_STAGE))
            crm_tasks.add(stage_task)
            stage_task.add_done_callback(crm_tasks.discard)
            logging.info("Cooperation inquiry detected chat=%s message=%s", chat_id, message.message_id)
            storage.log_event(chat_id, "info", "message", "message.cooperation", "Запрос о сотрудничестве/опте — сделка перенесена в «Нестандартный запрос»", {"text": text})
            schedule_idle_follow_up(chat_id, promised_hours)
            return

        # Клиент прямо спрашивает реквизиты/куда платить, либо уже подтвердил
        # заказ («можно оформить», «давайте», «беру») — в обоих случаях сразу
        # даём QR и просим чек/адрес/телефон одним сообщением, а не
        # собираем эти же данные через диалог с ИИ перед тем, как показать QR.
        last_assistant_message = next(
            (item.content for item in reversed(history) if item.role == "assistant"), ""
        )
        if (
            kind == "text"
            and not is_new_conversation
            and (
                wants_payment_info(text)
                or wants_to_order(text)
                or confirms_order_offer(text, last_assistant_message)
            )
            and storage.get_pending_payment(chat_id) is None
        ):
            # Скидка двухступенчатая: 13% (1290), затем 20% (1190).
            if storage.has_event(chat_id, "discount_offered_final"):
                default_amount = 1190
            elif storage.has_event(chat_id, "discount_offered"):
                default_amount = 1290
            else:
                default_amount = 1490
            storage.save_pending_payment(
                chat_id,
                order_summary="Клиент подтвердил заказ / запросил реквизиты оплаты",
                amount=default_amount,
                currency="KGS",
                customer_name=(message.from_user.full_name if message.from_user else None),
                customer_phone=None,
                city=None,
                address=None,
                customer_username=(message.from_user.username if message.from_user else None),
                source="Telegram",
            )
            if QR_CODE_PATH.exists():
                await bot.send_photo(chat_id, FSInputFile(QR_CODE_PATH))
            await send_delivery_order_request(chat_id)
            stored_request = "[голосовое: отправьте чек об оплате, адрес доставки и номер телефона]"
            storage.add_exchange(chat_id, message.message_id, text, stored_request, kind)
            storage.schedule_awaiting_payment_reminder(
                chat_id, time.time() + AWAITING_PAYMENT_REMINDER_DELAY_SECONDS
            )
            logging.info("Payment info requested chat=%s message=%s", chat_id, message.message_id)
            storage.log_event(chat_id, "info", "order", "order.qr_sent", "QR и запрос чек/адрес/телефон отправлены", {"text": text})
            return

        # Явное возражение («я подумаю», «дорого») — готовый ответ вместо
        # полноценной генерации: быстрее, дешевле, звучит последовательно.
        if kind == "text" and not is_new_conversation:
            objection = classify_objection(text)
            if objection:
                reply_text = templates.THINKING_REPLY if objection == "thinking" else templates.EXPENSIVE_REPLY
                await message.answer(reply_text)
                storage.add_exchange(chat_id, message.message_id, text, reply_text, kind)
                logging.info(
                    "Objection template chat=%s message=%s objection=%s",
                    chat_id, message.message_id, objection,
                )
                schedule_idle_follow_up(chat_id, promised_hours)
                return

        # Первый контакт — старый готовый сценарий из утверждённого скрипта:
        # информация о товаре и затем отдельный вопрос о городе.
        if is_new_conversation:
            await message.answer(templates.PRODUCT_INFO)
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(random.uniform(0.6, 1.0))
            await message.answer(templates.CITY_QUESTION)
            storage.add_exchange(
                chat_id, message.message_id,
                f"[{kind}] {text}" if kind != "text" else text,
                f"{templates.PRODUCT_INFO}\n\n{templates.CITY_QUESTION}", kind,
            )
            storage.schedule_daily_nudge(chat_id, time.time() + DAILY_NUDGE_INTERVAL_SECONDS)
            task = asyncio.create_task(idle_nudge(chat_id, message.message_id))
            follow_up_tasks.add(task)
            task.add_done_callback(follow_up_tasks.discard)
            return

        memory = storage.memory(chat_id)
        auto_result_photos = kind == "text" and should_send_results_after_city(
            text, [item.content for item in history]
        )
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        current_settings = effective_settings(storage, settings)

        # Роутер шаблонов: если вопрос дословно попадает под готовый ответ
        # (срок эффекта, состав) — шлём его напрямую, без полного цикла ИИ.
        if kind == "text":
            template_id = await assistant.route(text, history, model=current_settings["model"])
            if template_id == "discount_offer" and not storage.claim_event(chat_id, "discount_offered"):
                # 13% уже предлагали. Вторая просьба — последняя уступка, 20%;
                # третья и далее — фиксированный отказ (id решает код по
                # счётчику уступок, а не ИИ своим текстом).
                template_id = (
                    "discount_offer_final"
                    if storage.claim_event(chat_id, "discount_offered_final")
                    else "discount_reject"
                )
            template_text = ROUTED_TEMPLATES.get(template_id)
            if template_text:
                if template_id == "usage_instructions" and INSTRUCTIONS_IMAGE_PATH.exists():
                    await bot.send_photo(chat_id, FSInputFile(INSTRUCTIONS_IMAGE_PATH))
                elif template_id == "first_use_effect" and EFFECT_PHOTO_PATH.exists():
                    await bot.send_photo(chat_id, FSInputFile(EFFECT_PHOTO_PATH))
                await message.answer(template_text)
                storage.add_exchange(chat_id, message.message_id, text, template_text, kind)
                logging.info(
                    "Template routed chat=%s message=%s template=%s",
                    chat_id, message.message_id, template_id,
                )
                storage.log_event(chat_id, "info", "reply", "reply.routed_template", f"Готовый шаблон «{template_id}» вместо генерации", {"templateId": template_id})
                task = asyncio.create_task(idle_nudge(chat_id, message.message_id))
                follow_up_tasks.add(task)
                task.add_done_callback(follow_up_tasks.discard)
                schedule_idle_follow_up(chat_id, promised_hours)
                return

        # Клиент назвал город сразу после цены — готовый сценарий (результаты,
        # эффект, доставка, вопрос про оформление) вместо генерации ИИ:
        # иначе ИИ сам отвечает про доставку и просит оформить заказ, а этот
        # блок дублирует то же самое вторым сообщением.
        if auto_result_photos and storage.claim_event(chat_id, "results_after_city"):
            # Клиент мог написать город и заодно ещё что-то спросить в том же
            # сообщении — сперва полноценный ответ ИИ на этот вопрос, потом
            # тот же готовый хвост (доставка, результаты, эффект, CTA).
            # Неизвестный городу/стране тоже отвечает Luna — фиксированный
            # список нужен только для быстрых готовых сроков доставки.
            delivery_text = templates.delivery_template_for_city(text)
            if "?" in text or not delivery_text:
                reply = await assistant.reply(
                    text, history,
                    customer_memory=memory,
                    image_data_url=image_data_url,
                    manager_prompt=current_settings["managerPrompt"],
                    supervisor_prompt=current_settings["supervisorPrompt"],
                    model=current_settings["model"],
                )
                for index, part in enumerate(split_outgoing_messages(reply.text)):
                    if index:
                        await bot.send_chat_action(chat_id, ChatAction.TYPING)
                        await asyncio.sleep(min(2.0, max(0.7, len(part) / 55)) + random.uniform(0.0, 0.35))
                    await message.answer(part)
                storage.update_memory(chat_id, reply.supervisor.customer_memory, reply.supervisor.order_summary)
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(random.uniform(0.6, 1.0))

            # Сперва прямой ответ на то, что подразумевает названный город
            # (доставка), и только потом результаты — не наоборот.
            if delivery_text:
                await message.answer(delivery_text)
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(random.uniform(0.7, 1.1))
            await message.answer("Вот результат нашего клиента до и после применения 👇")
            await bot.send_media_group(chat_id, media=result_photo_group())
            await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
            await asyncio.sleep(random.uniform(0.8, 1.3))
            await bot.send_voice(chat_id, FSInputFile(FIRST_USE_EFFECT_VOICE_PATH))
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(random.uniform(0.7, 1.1))
            await message.answer(templates.ORDER_STOCK_CTA)
            stored_reply = "\n\n".join(filter(None, [
                delivery_text,
                "[фото до/после]",
                "[голосовое: эффект после первого применения]",
                templates.ORDER_STOCK_CTA,
            ]))
            storage.add_exchange(chat_id, message.message_id, text, stored_reply, kind)
            task = asyncio.create_task(follow_up_after_results(chat_id, message.message_id))
            follow_up_tasks.add(task)
            task.add_done_callback(follow_up_tasks.discard)
            schedule_idle_follow_up(chat_id, promised_hours)
            return

        reply = await assistant.reply(
            text,
            history,
            customer_memory=memory,
            image_data_url=image_data_url,
            manager_prompt=current_settings["managerPrompt"],
            supervisor_prompt=current_settings["supervisorPrompt"],
            model=current_settings["model"],
        )
        logging.info(
            "AI reply ready chat=%s message=%s",
            chat_id,
            message.message_id,
        )
        parts = split_outgoing_messages(reply.text)
        for index, part in enumerate(parts):
            if index:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                typing_delay = min(2.0, max(0.7, len(part) / 55))
                await asyncio.sleep(typing_delay + random.uniform(0.0, 0.35))
            await message.answer(part)
        logging.info(
            "Telegram reply sent chat=%s message=%s parts=%s",
            chat_id,
            message.message_id,
            len(parts),
        )
        storage.log_event(
            chat_id, "info", "reply", "reply.ai_generated",
            "ИИ сгенерировал и отправил ответ",
            {
                "parts": len(parts),
                "reviewStatus": reply.supervisor.status,
                "expectedClientReply": reply.supervisor.expected_client_reply,
            },
        )
        stored_reply = "\n\n".join(parts)
        storage.add_exchange(
            chat_id,
            message.message_id,
            f"[{kind}] {text}" if kind != "text" else text,
            stored_reply,
            kind,
        )
        storage.update_memory(
            chat_id,
            reply.supervisor.customer_memory,
            reply.supervisor.order_summary,
        )
        pending_payment = storage.get_pending_payment(chat_id)
        if pending_payment is not None:
            author = message.from_user
            storage.update_pending_payment_details(
                chat_id,
                order_summary=reply.supervisor.order_summary,
                customer_name=(
                    reply.supervisor.order.customer_name
                    or pending_payment.get("customer_name")
                    or (author.full_name if author else None)
                ),
                customer_phone=reply.supervisor.order.contact_phone,
                city=reply.supervisor.order.city,
                address=reply.supervisor.order.address,
            )
            updated_payment = storage.get_pending_payment(chat_id)
            if updated_payment and updated_payment.get("receipt_verified"):
                await complete_verified_order(message, updated_payment)
                return
        task = asyncio.create_task(idle_nudge(chat_id, message.message_id))
        follow_up_tasks.add(task)
        task.add_done_callback(follow_up_tasks.discard)
        order_triggered = await push_order(message, reply.supervisor)
        if not order_triggered:
            if kind == "text" and wants_to_order(text):
                storage.schedule_order_incomplete_nudge(chat_id, time.time())
            else:
                schedule_idle_follow_up(chat_id, promised_hours)
        if kind == "text" and wants_result_photos(text):
            await message.answer("Вот результат нашего клиента до и после применения 👇")
            await bot.send_media_group(chat_id, media=result_photo_group())
            task = asyncio.create_task(
                follow_up_after_results(chat_id, message.message_id)
            )
            follow_up_tasks.add(task)
            task.add_done_callback(follow_up_tasks.discard)
        elif kind == "text" and (wants_review_photos(text) or confirms_review_offer(text, last_assistant_message)):
            await message.answer("Вот отзывы наших клиентов 👇")
            await bot.send_media_group(chat_id, media=review_photo_group())
            storage.log_event(chat_id, "info", "reply", "reply.reviews_sent", "Отправлены фото отзывов клиентов")

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        async with locks[message.chat.id]:
            if not await claim(message, rate_limit=False):
                return
            chat_id = message.chat.id
            storage.clear_chat(chat_id)
            await message.answer(templates.PRODUCT_INFO)
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(random.uniform(0.6, 1.0))
            await message.answer(templates.CITY_QUESTION)
            storage.add_exchange(
                chat_id, message.message_id, "/start",
                f"{templates.PRODUCT_INFO}\n\n{templates.CITY_QUESTION}", "command",
            )
            storage.schedule_daily_nudge(chat_id, time.time() + DAILY_NUDGE_INTERVAL_SECONDS)
            task = asyncio.create_task(idle_nudge(chat_id, message.message_id))
            follow_up_tasks.add(task)
            task.add_done_callback(follow_up_tasks.discard)

    @dp.message(Command("reset"))
    async def reset(message: Message) -> None:
        async with locks[message.chat.id]:
            if not await claim(message, rate_limit=False):
                return
            storage.clear_chat(message.chat.id)
            await message.answer("Диалог и сохранённая память очищены.")

    @dp.message(Command("delivery_group"))
    async def configure_delivery_group(message: Message) -> None:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.answer("Эту команду нужно отправить в группе доставки.")
            return
        if message.from_user is None:
            return
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            await message.answer("Подключить группу может только её администратор.")
            return
        storage.set_bot_setting(DELIVERY_GROUP_SETTING_KEY, str(message.chat.id))
        await message.answer(
            "Группа доставки подключена ✅\n"
            "После подтверждения оплаты сюда будут приходить данные клиента и чек."
        )
        logging.info("Delivery group configured chat=%s by=%s", message.chat.id, message.from_user.id)

    async def handle_receipt_file(
        message: Message, pending: dict, *, file_id: str, file_bytes: bytes, kind: str
    ) -> None:
        """Ждём чек по активному заказу (фото или PDF) — проверяем сумму
        вместо обычного разбора вложения ИИ-менеджером."""
        chat_id = message.chat.id
        try:
            encoded = base64.b64encode(file_bytes).decode("ascii")
            mime = "application/pdf" if kind == "pdf" else "image/jpeg"
            today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + BISHKEK_UTC_OFFSET_SECONDS))
            result = await assistant.verify_receipt(
                f"data:{mime};base64,{encoded}",
                today,
                kind=kind,
            )
        except Exception:
            logging.exception("Receipt verification crashed chat=%s", chat_id)
            result = {}

        if all(result.get(key) for key in ("is_real_receipt_screenshot", "amount_ok", "recipient_ok", "date_ok")):
            storage.mark_pending_payment_receipt(chat_id, file_id, kind)
            updated_payment = storage.get_pending_payment(chat_id) or pending
            missing = missing_delivery_details(updated_payment)
            if missing:
                await message.answer(
                    "Чек подтверждён ✅ Теперь отправьте, пожалуйста: "
                    + " и ".join(missing)
                    + ". Имя не требуется."
                )
                storage.schedule_awaiting_payment_reminder(
                    chat_id, time.time() + AWAITING_PAYMENT_REMINDER_DELAY_SECONDS
                )
                storage.log_event(
                    chat_id, "info", "order", "order.receipt_waiting_details",
                    "Чек подтверждён, бот ждёт адрес и телефон", {"missing": missing},
                )
                return
            await complete_verified_order(message, updated_payment)
            storage.log_event(chat_id, "info", "order", "order.receipt_verified", "Чек прошёл проверку, заказ подтверждён")
        else:
            storage.cancel_nudge_follow_ups(chat_id)
            await push_receipt_mismatch_to_delivery_group(pending, file_id, kind, result, today)
            receipt_url = await telegram_file_url(file_id)
            if receipt_url:
                await crm.attach_receipt(chat_id, receipt_url)
            storage.log_event(
                chat_id, "warn", "order", "order.receipt_mismatch",
                "Чек не прошёл проверку; клиенту сообщение не отправлено", result,
            )

    @dp.message(F.photo)
    async def handle_photo(message: Message) -> None:
        async with locks[message.chat.id]:
            if not await claim(message):
                return
            pending_payment = storage.get_pending_payment(message.chat.id)
            if pending_payment is not None:
                history_text = [item.content for item in storage.history(message.chat.id)]
                phone, address, city = extract_delivery_details(message.caption or "", *history_text)
                storage.update_pending_payment_details(
                    message.chat.id, order_summary=None, customer_name=None,
                    customer_phone=phone, city=city, address=address,
                )
                pending_payment = storage.get_pending_payment(message.chat.id) or pending_payment
                photo_bytes = await download(message.photo[-1].file_id)
                await handle_receipt_file(
                    message, pending_payment,
                    file_id=message.photo[-1].file_id, file_bytes=photo_bytes, kind="image",
                )
                return
            try:
                photo = await download(message.photo[-1].file_id)
                encoded = base64.b64encode(photo).decode("ascii")
                text = message.caption.strip() if message.caption else PHOTO_PROMPT
                await process(
                    message,
                    text,
                    kind="photo",
                    image_data_url=f"data:image/jpeg;base64,{encoded}",
                )
            except Exception:
                logging.exception("Photo processing failed")
                await message.answer(
                    "Не получилось обработать фото. "
                    "Попробуйте отправить его ещё раз немного позже."
                )

    @dp.message(F.document)
    async def handle_document(message: Message) -> None:
        async with locks[message.chat.id]:
            if not await claim(message):
                return
            pending_payment = storage.get_pending_payment(message.chat.id)
            document = message.document
            is_pdf = bool(
                document
                and (document.mime_type == "application/pdf" or (document.file_name or "").lower().endswith(".pdf"))
            )
            if pending_payment is not None and is_pdf:
                history_text = [item.content for item in storage.history(message.chat.id)]
                phone, address, city = extract_delivery_details(message.caption or "", *history_text)
                storage.update_pending_payment_details(
                    message.chat.id, order_summary=None, customer_name=None,
                    customer_phone=phone, city=city, address=address,
                )
                pending_payment = storage.get_pending_payment(message.chat.id) or pending_payment
                try:
                    pdf_bytes = await download(document.file_id)
                except Exception:
                    logging.exception("Receipt PDF download failed chat=%s", message.chat.id)
                    await message.answer(templates.RECEIPT_MISMATCH)
                    return
                await handle_receipt_file(
                    message, pending_payment,
                    file_id=document.file_id, file_bytes=pdf_bytes, kind="pdf",
                )
                return
            if pending_payment is not None:
                await message.answer(templates.RECEIPT_MISMATCH)
                return
            await message.answer(
                "Такой файл пока не могу обработать. Опишите вопрос текстом "
                "или пришлите фото."
            )

    @dp.message(F.voice | F.audio)
    async def handle_audio(message: Message) -> None:
        async with locks[message.chat.id]:
            if not await claim(message):
                return
            media = message.voice or message.audio
            if not media:
                return
            if media.duration and media.duration > settings.max_voice_seconds:
                await message.answer(
                    f"Голосовое слишком длинное. Отправьте запись до "
                    f"{settings.max_voice_seconds // 60} минут."
                )
                return
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                audio = await download(media.file_id)
                filename = getattr(media, "file_name", None) or "voice.ogg"
                transcript = await assistant.transcribe(
                    audio,
                    filename,
                    settings.transcription_model,
                )
                await process(
                    message,
                    transcript,
                    kind="voice",
                )
            except Exception:
                logging.exception("Voice processing failed")
                await message.answer(
                    "Не получилось распознать голосовое. "
                    "Отправьте его ещё раз или напишите вопрос текстом."
                )

    async def debounced_text_process(message: Message) -> None:
        chat_id = message.chat.id
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # Клиент прислал ещё одно сообщение до истечения окна ожидания —
            # его обработчик уже запланировал новый debounce-таск и допишет
            # текст в pending_texts, дальше ничего делать не нужно.
            return
        # Убираем себя из реестра ДО захвата lock: если во время генерации
        # ответа придёт новое сообщение, его обработчик не должен отменять
        # уже выполняющийся вызов AI — вместо этого он поставит в очередь
        # свой собственный debounce-таск, который дождётся освобождения lock.
        debounce_tasks.pop(chat_id, None)
        texts = pending_texts.pop(chat_id, None)
        if not texts:
            return
        combined_text = "\n".join(texts)
        async with locks[chat_id]:
            try:
                await process(message, combined_text, kind="text")
            except Exception:
                logging.exception(
                    "Text processing failed chat=%s message=%s",
                    chat_id,
                    message.message_id,
                )
                await message.answer(
                    "Не получилось обработать сообщение. "
                    "Отправьте его, пожалуйста, ещё раз."
                )

    @dp.message(F.text)
    async def handle_text(message: Message) -> None:
        if not message.text:
            return
        if not await claim(message):
            return
        chat_id = message.chat.id
        pending_texts[chat_id].append(message.text)
        previous_task = debounce_tasks.get(chat_id)
        if previous_task is not None:
            previous_task.cancel()
        debounce_tasks[chat_id] = asyncio.create_task(debounced_text_process(message))

    @dp.message()
    async def unsupported(message: Message) -> None:
        async with locks[message.chat.id]:
            if not await claim(message):
                return
            await message.answer(
                "Я могу обработать текст, фотографию или голосовое сообщение."
            )

    # /internal/* HTTP-слой для CRM (настройки бота + лаборатория) поднимается
    # рядом с long polling в этом же процессе, на отдельном порту — вебхуки не
    # трогаем, это чисто аддитивная ручка для внутреннего использования.
    api_app = create_app(settings, storage, assistant, bot)
    runner = web.AppRunner(api_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
    await site.start()
    logging.info("Internal HTTP API listening on port %s", settings.port)

    daily_nudge_task = asyncio.create_task(check_daily_nudges())
    nudge_follow_up_task = asyncio.create_task(check_nudge_follow_ups())

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        daily_nudge_task.cancel()
        nudge_follow_up_task.cancel()
        for task in follow_up_tasks:
            task.cancel()
        for task in debounce_tasks.values():
            task.cancel()
        if crm_tasks:
            # Заказ терять нельзя: даём отправке доехать до выхода.
            await asyncio.wait(crm_tasks, timeout=REQUEST_TIMEOUT)
        await runner.cleanup()
        storage.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
