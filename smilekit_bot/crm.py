"""Отправка оформленного заказа в CRM Smilekit.

Работает по принципу «отправил и забыл»: медленная или лежащая CRM не должна
задерживать ответ клиенту, поэтому запрос уходит фоновой задачей, а любая
ошибка попадает только в лог.

Идемпотентность: external_key = telegram:<chat_id>:order:<отпечаток состава>.
Отпечаток считается по структурным полям заказа и НЕ включает текст сводки —
иначе каждая перефразировка гипервизора заводила бы новый заказ. Повторные
подтверждения одного и того же заказа дают тот же ключ, и CRM их склеивает;
реально изменившийся состав даёт новый ключ и новый заказ.

Зависимостей нет: HTTP через urllib из стандартной библиотеки в отдельном
потоке (запрос блокирующий, событийный цикл он не занимает).
"""

import asyncio
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request

from .models import OrderDetails, SupervisorResult
from .sales import MANAGER_CONTACT


LOGGER = logging.getLogger(__name__)

# Жалобы/возврат/опт-сотрудничество — не обычная воронка продажи, деал
# переносится в отдельный этап на ручной разбор менеджером.
NON_STANDARD_REQUEST_STAGE = "non_standard_request"

ORDER_PATH = "/api/internal/orders"
ORDER_RECEIPT_PATH = "/api/internal/orders/attach-receipt"
DEALS_PATH = "/api/internal/deals"
NOTIFY_PATH = "/api/internal/notify"
REQUEST_TIMEOUT = 10.0
CURRENCIES = {"KGS", "USD", "RUB"}
MAX_QUANTITY = 1000
MAX_TEXT = 200
MAX_SUMMARY = 4000


def _text(value: object, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def _phone(value: object) -> str | None:
    """Телефон клиента. Номер менеджера сюда попасть не должен.

    Промпт это запрещает, но модель ошибается: заказ с номером менеджера
    вместо номера клиента менеджер будет разбирать дольше, чем пустое поле.
    """
    cleaned = _text(value, 32)
    if cleaned is None:
        return None
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return None
    if MANAGER_CONTACT in digits or MANAGER_CONTACT[-9:] in digits:
        return None
    return cleaned


def _quantity(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    if number < 1:
        return None
    return min(number, MAX_QUANTITY)


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return round(float(value), 2)


def _currency(value: object) -> str | None:
    cleaned = _text(value, 8)
    if cleaned is None:
        return None
    upper = cleaned.upper()
    return upper if upper in CURRENCIES else None


def order_fingerprint(order: OrderDetails) -> str:
    """Отпечаток состава заказа: одинаковый заказ — одинаковый ключ."""
    parts = [
        str(_quantity(order.quantity)),
        str(_amount(order.total_amount)),
        str(_currency(order.currency)),
        (_text(order.city) or "").casefold(),
        re.sub(r"\D", "", _phone(order.contact_phone) or ""),
        (_text(order.contact_username) or "").casefold().lstrip("@"),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def build_order_payload(
    chat_id: int,
    supervisor: SupervisorResult,
    *,
    customer_name: str | None = None,
    telegram_username: str | None = None,
) -> dict | None:
    """Собирает тело запроса или возвращает None, если заказа ещё нет.

    Признак заказа — непустой order_summary И реальный телефон клиента.
    Гипервизор иногда заполняет order_summary рано (клиент просто согласился
    на скидку/покупку, но телефон ещё не назвал) — без проверки телефона
    бот в этот момент уже шлёт QR на оплату, хотя сам же ещё просит имя и
    номер. Телефон — минимальный сигнал, что данные для доставки реально
    начали собираться, а не просто устное согласие.
    """
    summary = _text(supervisor.order_summary, MAX_SUMMARY)
    if summary is None:
        return None
    order = supervisor.order
    phone = _phone(order.contact_phone)
    if phone is None:
        return None

    username = _text(order.contact_username, 120) or _text(telegram_username, 120)
    return {
        "externalKey": f"telegram:{chat_id}:order:{order_fingerprint(order)}",
        "source": "telegram_bot",
        "customerName": _text(order.customer_name) or _text(customer_name),
        "customerPhone": phone,
        "customerUsername": username.lstrip("@") if username else None,
        "telegramChatId": chat_id,
        "quantity": _quantity(order.quantity),
        "totalAmount": _amount(order.total_amount),
        "currency": _currency(order.currency),
        "city": _text(order.city, 120),
        "address": _text(order.address, 500),
        "summary": summary,
    }


_READY_TO_BUY_RE = re.compile(
    r"оформ(?:ить|ляйте)|заказ(?:ать|ываю)?|беру\b|покупаю\b|заброниру|резервиру", re.IGNORECASE
)
_INTEREST_CONFIRMED_RE = re.compile(
    r"хочу\b|подходит|устраивает|интересует|готов\s+(?:взять|купить)|давайте", re.IGNORECASE
)
_NEED_IDENTIFIED_RE = re.compile(
    r"полоск|отбелив|улыбк|зуб|цена|сколько\s+стоит|состав|инструкц|"
    r"противопоказ|результат|скидк|доставк|коробк",
    re.IGNORECASE,
)


def stage_action_for_inbound(text: str) -> str | None:
    """Намерение клиента по входящему сообщению → этап воронки в CRM.

    Логика симметрична server/services/crm.js витрины Мостового (та же
    трёхуровневая эвристика: явное решение купить → согласие/интерес →
    вопрос по товару), словарь адаптирован под полоски Smilekit вместо
    электроники.
    """
    value = (text or "").replace("ё", "е")
    if _READY_TO_BUY_RE.search(value):
        return "ready_to_buy"
    if _INTEREST_CONFIRMED_RE.search(value):
        return "interest_confirmed"
    if _NEED_IDENTIFIED_RE.search(value):
        return "need_identified"
    return None


def build_deal_payload(
    chat_id: int,
    *,
    customer_name: str | None = None,
    telegram_username: str | None = None,
) -> dict:
    """Тело запроса для завода сделки при первом сообщении клиента."""
    username = _text(telegram_username, 120)
    return {
        "externalKey": f"telegram:{chat_id}",
        "source": "telegram",
        "customerName": _text(customer_name),
        "customerUsername": username.lstrip("@") if username else None,
    }


class CrmClient:
    """Клиент CRM. Без настроек молча выключен — падать боту не из-за чего."""

    def __init__(self, base_url: str, token: str, timeout: float = REQUEST_TIMEOUT) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def _request(self, method: str, path: str, payload: dict) -> None:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-internal-token": self.token,
            },
            method=method,
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()

    async def send_order(self, payload: dict) -> bool:
        """Отправляет заказ. Никогда не бросает исключение наружу."""
        if not self.enabled:
            return False
        try:
            await asyncio.to_thread(self._request, "POST", ORDER_PATH, payload)
        except urllib.error.HTTPError as error:
            LOGGER.warning(
                "CRM rejected order %s: HTTP %s", payload.get("externalKey"), error.code
            )
            return False
        except Exception:
            LOGGER.exception("CRM order delivery failed for %s", payload.get("externalKey"))
            return False
        LOGGER.info("Order %s delivered to CRM", payload.get("externalKey"))
        return True

    async def attach_receipt(self, telegram_chat_id: int, media_url: str) -> bool:
        """Прикрепляет ссылку на чек к последнему заказу этого чата в CRM —
        менеджер видит его прямо в карточке заказа/сделки, а не только в
        группе доставки. Никогда не бросает исключение наружу."""
        if not self.enabled:
            return False
        try:
            await asyncio.to_thread(
                self._request, "POST", ORDER_RECEIPT_PATH,
                {"telegramChatId": telegram_chat_id, "mediaUrl": media_url},
            )
        except Exception:
            LOGGER.exception("CRM receipt attach failed for chat=%s", telegram_chat_id)
            return False
        return True

    async def create_deal(self, payload: dict) -> bool:
        """Заводит сделку в «Неразобранном» при первом сообщении клиента.

        Никогда не бросает исключение наружу: если CRM недоступна, диалог
        просто не попадёт в воронку, но клиент всё равно получит ответ.
        """
        if not self.enabled:
            return False
        try:
            await asyncio.to_thread(self._request, "POST", DEALS_PATH, payload)
        except urllib.error.HTTPError as error:
            LOGGER.warning(
                "CRM rejected deal %s: HTTP %s", payload.get("externalKey"), error.code
            )
            return False
        except Exception:
            LOGGER.exception("CRM deal creation failed for %s", payload.get("externalKey"))
            return False
        return True

    async def advance_stage(self, chat_id: int, action: str) -> bool:
        """Двигает сделку по воронке. Никогда не бросает исключение наружу.

        HTTP 404 (сделки ещё нет — гонка с create_deal на самом первом
        сообщении) — не ошибка, просто пропускаем: следующее сообщение
        клиента продвинет уже существующую сделку.
        """
        if not self.enabled:
            return False
        payload = {"externalKey": f"telegram:{chat_id}", "action": action}
        try:
            await asyncio.to_thread(self._request, "PATCH", DEALS_PATH, payload)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                LOGGER.warning("CRM rejected stage advance %s: HTTP %s", action, error.code)
            return False
        except Exception:
            LOGGER.exception("CRM stage advance failed chat=%s action=%s", chat_id, action)
            return False
        return True

    async def notify_important(self, *, title: str, body: str, chat_id: int) -> bool:
        """Важное уведомление в CRM — жалоба или сообщение, которое должен
        разобрать человек (не автоответ). Никогда не бросает исключение
        наружу: сбой не должен мешать боту ответить клиенту."""
        if not self.enabled:
            return False
        payload = {
            "type": "system",
            "title": title,
            "body": body,
            "isImportant": True,
            "sourceType": "telegram_bot",
            "sourceId": f"telegram:{chat_id}",
        }
        try:
            await asyncio.to_thread(self._request, "POST", NOTIFY_PATH, payload)
        except Exception:
            LOGGER.exception("CRM notify failed chat=%s title=%s", chat_id, title)
            return False
        return True
