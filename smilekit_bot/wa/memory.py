"""Общая память диалога — одна на чат, её читают и пишут все роли.

Разговорный этап (stage) и состояние заказа (order.status) хранятся
раздельно: возврат к вопросам о товаре не меняет заказ. Полная переписка
лежит отдельно в wa_messages и памятью не заменяется.
"""

import json
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel, Field


class Geo(BaseModel):
    zone: str = ""          # см. rules.ZONES; "" — город ещё не называли
    place: str = ""         # как назвал клиент, в именительном падеже
    place_to: str = ""      # форма после «доставка в …»
    clarify_asked: int = 0
    corrections: list[str] = Field(default_factory=list)


class Presented(BaseModel):
    price: bool = False
    city_question: bool = False
    delivery_for: str = ""  # для какого места уже отправлен текст доставки
    photo: bool = False
    media_skipped: bool = False  # клиент сразу перешёл к заказу — фото и голосовое не досылаем
    voices: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)


class Question(BaseModel):
    id: int
    text: str
    topic: str = ""
    status: str = "open"    # open | answered | unknown
    counted: bool = False


class Offer(BaseModel):
    """Последнее предложение бота — к нему относится короткое «да»."""

    kind: str               # order | discount | composition | instruction
    text: str
    price: int | None = None
    at: float = 0


class Sales(BaseModel):
    answered_in_cycle: int = 0
    offers_made: int = 0
    temporary_declines: int = 0
    last_offer: Offer | None = None
    last_offer_reply: str = ""
    consent_text: str = ""
    consent_at: float = 0
    consent_revoked: bool = False
    order_intent_pending: bool = False  # хотел купить, но не хватало города
    offer_on_city: bool = False  # хотел купить ещё до того, как увидел цену: после города предложим оформить


class Price(BaseModel):
    currency: str = "сом"
    listed: int = 1490
    offered: int = 1490
    agreed: int | None = None
    discount_level: int = 0
    discount_reason: str = ""


class Order(BaseModel):
    # none → collecting → registered → handed_off; отдельно cancelled.
    status: str = "none"
    quantity: int | None = None
    quantity_assumed: bool = False
    address: str = ""
    phone: str = ""
    recipient_name: str = ""
    payment: str = ""       # full | cod | prepay
    # none | check_ok (чек прошёл автопроверку) | manual_review.
    # Ни одно значение не означает подтверждённое поступление денег.
    receipt: str = "none"
    receipt_note: str = ""
    receipt_ids: list[str] = Field(default_factory=list)
    order_id: int | None = None
    handoff: str = ""       # "" | ok | failed
    handoff_attempts: int = 0


class Wait(BaseModel):
    last_bot_sent_at: float = 0
    last_client_at: float = 0
    neutral_sent_at: float = 0
    chain: str = "silence"  # silence | second_decline
    chain_anchor: float = 0
    callback_at: float = 0
    checkout_reminder_sent: bool = False
    stopped: bool = False   # цепочка отработала — ждём ответа клиента
    retry_after: float = 0  # напоминание не ушло — до этого времени не пробуем снова
    last_neutral_at: float = 0  # когда уходило последнее нейтральное напоминание (не сбрасывается ответом клиента)


class Control(BaseModel):
    mode: str = "bot"       # bot | manager
    stop_sales: bool = False
    handoff_reason: str = ""
    handoff_pending: bool = False  # карточка менеджеру не дошла — нужен повтор


class DialogMemory(BaseModel):
    chat_id: str
    stage: str = "contact"  # contact | consult | offer | wait | checkout | complete
    language: str = "ru"
    epoch: int = 0          # растёт с каждой репликой клиента
    geo: Geo = Field(default_factory=Geo)
    presented: Presented = Field(default_factory=Presented)
    questions: list[Question] = Field(default_factory=list)
    sales: Sales = Field(default_factory=Sales)
    price: Price = Field(default_factory=Price)
    order: Order = Field(default_factory=Order)
    wait: Wait = Field(default_factory=Wait)
    control: Control = Field(default_factory=Control)
    seen_ids: list[str] = Field(default_factory=list)

    def open_questions(self) -> list[Question]:
        return [q for q in self.questions if q.status == "open"]

    def facts(self) -> str:
        """Короткая сводка для ролей. Только установленное; догадок нет."""
        lines = [f"этап: {self.stage}", f"язык клиента: {self.language}"]
        if self.geo.zone:
            lines.append(f"доставка: {self.geo.place} (зона {self.geo.zone})")
        else:
            lines.append("город доставки неизвестен")
        price = f"цена {self.price.offered} {self.price.currency}"
        if self.price.agreed:
            price += f", согласована {self.price.agreed}"
        lines.append(price)
        if self.sales.last_offer:
            lines.append(f"последнее предложение бота ({self.sales.last_offer.kind}): «{self.sales.last_offer.text}»")
        if self.order.status != "none":
            lines.append(
                f"заказ: {self.order.status}, оплата {self.order.payment or 'не выбрана'}, "
                f"адрес {'есть' if self.order.address else 'нет'}, телефон {'есть' if self.order.phone else 'нет'}, "
                f"чек {self.order.receipt}"
            )
        if self.presented.materials:
            lines.append("уже отправлено: " + ", ".join(self.presented.materials))
        return "\n".join(lines)


class Store:
    """Локальное хранилище (SQLite): память, переписка, журнал, заказы."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Один процесс, вызовы сериализует вызывающий код (см. wa_api.py) — поэтому
        # соединением можно пользоваться из рабочего потока.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS wa_memory (chat_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wa_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, ts REAL, sender TEXT, kind TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS wa_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, ts REAL, agent TEXT, event TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS wa_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, key TEXT UNIQUE, card TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS wa_pending (id TEXT PRIMARY KEY, chat_id TEXT, data TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS wa_i18n (lang TEXT, src TEXT, dst TEXT, PRIMARY KEY (lang, src));
            """
        )

    def load(self, chat_id: str) -> DialogMemory:
        row = self.db.execute("SELECT data FROM wa_memory WHERE chat_id = ?", (chat_id,)).fetchone()
        return DialogMemory.model_validate_json(row[0]) if row else DialogMemory(chat_id=chat_id)

    def save(self, memory: DialogMemory) -> None:
        self.db.execute(
            "INSERT INTO wa_memory (chat_id, data) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET data = excluded.data",
            (memory.chat_id, memory.model_dump_json()),
        )
        self.db.commit()

    def add_message(self, chat_id: str, sender: str, kind: str, text: str, ts: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO wa_messages (chat_id, ts, sender, kind, text) VALUES (?, ?, ?, ?, ?)",
            (chat_id, ts or time.time(), sender, kind, text),
        )
        self.db.commit()

    def recent(self, chat_id: str, limit: int = 12) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT sender, text FROM wa_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
        return list(reversed(rows))

    def log(self, chat_id: str, agent: str, event: str, detail: str = "", ts: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO wa_log (chat_id, ts, agent, event, detail) VALUES (?, ?, ?, ?, ?)",
            (chat_id, ts or time.time(), agent, event, detail),
        )
        self.db.commit()

    def journal(self, chat_id: str, limit: int = 40) -> list[tuple[float, str, str, str]]:
        rows = self.db.execute(
            "SELECT ts, agent, event, detail FROM wa_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        ).fetchall()
        return list(reversed(rows))

    def register_order(self, chat_id: str, key: str, card: dict) -> tuple[int, bool]:
        """Создаёт заказ один раз на ключ. Возвращает (id, создан ли сейчас)."""
        row = self.db.execute("SELECT id FROM wa_orders WHERE key = ?", (key,)).fetchone()
        if row:
            return row[0], False
        cursor = self.db.execute(
            "INSERT INTO wa_orders (chat_id, key, card, created) VALUES (?, ?, ?, ?)",
            (chat_id, key, json.dumps(card, ensure_ascii=False), time.time()),
        )
        self.db.commit()
        return int(cursor.lastrowid), True

    def orders(self, chat_id: str) -> list[dict]:
        rows = self.db.execute("SELECT id, card FROM wa_orders WHERE chat_id = ?", (chat_id,)).fetchall()
        return [{"id": row[0], **json.loads(row[1])} for row in rows]

    # Исходящие, которые отдали транспорту и ждём подтверждения отправки.
    def put_pending(self, out_id: str, chat_id: str, data: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO wa_pending (id, chat_id, data, created) VALUES (?, ?, ?, ?)",
            (out_id, chat_id, json.dumps(data, ensure_ascii=False), time.time()),
        )
        self.db.commit()

    def take_pending(self, out_id: str, remove: bool) -> dict | None:
        row = self.db.execute("SELECT data FROM wa_pending WHERE id = ?", (out_id,)).fetchone()
        if row and remove:
            self.db.execute("DELETE FROM wa_pending WHERE id = ?", (out_id,))
            self.db.commit()
        return json.loads(row[0]) if row else None

    def chat_ids(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT chat_id FROM wa_memory").fetchall()]

    def forget(self, chat_id: str) -> None:
        for table in ("wa_memory", "wa_messages", "wa_log", "wa_pending"):
            self.db.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    # Переводы готовых текстов: один раз перевели — дальше всем одинаково.
    def translation(self, lang: str, src: str) -> str | None:
        row = self.db.execute("SELECT dst FROM wa_i18n WHERE lang = ? AND src = ?", (lang, src)).fetchone()
        return row[0] if row else None

    def save_translation(self, lang: str, src: str, dst: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO wa_i18n (lang, src, dst) VALUES (?, ?, ?)", (lang, src, dst))
        self.db.commit()
