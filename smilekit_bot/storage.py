import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import ChatMessage


class BotStorage:
    """Small persistent store for idempotency, dialogue memory and orders."""

    def __init__(self, path: str) -> None:
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS processed_messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (chat_id, telegram_message_id, role)
                );
                CREATE INDEX IF NOT EXISTS messages_chat_idx
                    ON messages(chat_id, id);
                CREATE TABLE IF NOT EXISTS customer_memory (
                    chat_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    chat_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_events (
                    chat_id INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rate_events_chat_idx
                    ON rate_events(chat_id, created_at);
                CREATE TABLE IF NOT EXISTS sent_events (
                    chat_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, event)
                );
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_nudges (
                    chat_id INTEGER PRIMARY KEY,
                    next_due_at REAL NOT NULL,
                    sent_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS nudge_follow_ups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('hours', 'day', 'last', 'order_incomplete', 'promised', 'awaiting_payment')),
                    due_at REAL NOT NULL,
                    sent_at REAL
                );
                CREATE INDEX IF NOT EXISTS nudge_follow_ups_pending_idx
                    ON nudge_follow_ups(chat_id, sent_at);
                CREATE TABLE IF NOT EXISTS pending_payments (
                    chat_id INTEGER PRIMARY KEY,
                    order_summary TEXT NOT NULL,
                    amount REAL,
                    currency TEXT,
                    customer_name TEXT,
                    customer_phone TEXT,
                    city TEXT,
                    address TEXT,
                    customer_username TEXT,
                    source TEXT,
                    receipt_verified INTEGER NOT NULL DEFAULT 0,
                    receipt_file_id TEXT,
                    receipt_kind TEXT,
                    reminder_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paused_chats (
                    chat_id INTEGER PRIMARY KEY,
                    paused_at REAL NOT NULL,
                    paused_by TEXT
                );
                CREATE TABLE IF NOT EXISTS bot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    level TEXT NOT NULL CHECK (level IN ('info', 'warn', 'error')),
                    stage TEXT NOT NULL,
                    event TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    details TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bot_events_id_idx ON bot_events(id);
                """
            )
            self._db.commit()
            self._migrate_nudge_follow_ups_promised_kind()
            self._migrate_messages_sender()
            self._migrate_pending_payments_address()

    def _migrate_pending_payments_address(self) -> None:
        with self._lock:
            cols = [row["name"] for row in self._db.execute("PRAGMA table_info(pending_payments)")]
            if "address" not in cols:
                self._db.execute("ALTER TABLE pending_payments ADD COLUMN address TEXT")
            if "customer_username" not in cols:
                self._db.execute("ALTER TABLE pending_payments ADD COLUMN customer_username TEXT")
            if "source" not in cols:
                self._db.execute("ALTER TABLE pending_payments ADD COLUMN source TEXT")
            if "receipt_verified" not in cols:
                self._db.execute("ALTER TABLE pending_payments ADD COLUMN receipt_verified INTEGER NOT NULL DEFAULT 0")
            if "receipt_file_id" not in cols:
                self._db.execute("ALTER TABLE pending_payments ADD COLUMN receipt_file_id TEXT")
            if "receipt_kind" not in cols:
                self._db.execute("ALTER TABLE pending_payments ADD COLUMN receipt_kind TEXT")
            self._db.commit()

    def _migrate_messages_sender(self) -> None:
        """sender различает автора исходящего: bot | operator (role остаётся
        как есть — для истории ИИ оба выглядят репликой ассистента).
        Историческим строкам до миграции ставим bot/client по role."""
        with self._lock:
            cols = [row["name"] for row in self._db.execute("PRAGMA table_info(messages)")]
            if "sender" not in cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN sender TEXT")
                self._db.execute(
                    "UPDATE messages SET sender = CASE role WHEN 'user' THEN 'client' ELSE 'bot' END"
                )
                self._db.commit()

    def _migrate_nudge_follow_ups_promised_kind(self) -> None:
        """CREATE TABLE IF NOT EXISTS не меняет CHECK на уже существующей
        таблице — на проде она была заведена без 'promised'/'awaiting_payment'.
        Пересоздаём таблицу с сохранением данных, если старый CHECK ещё не
        обновлён."""
        with self._lock:
            row = self._db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'nudge_follow_ups'"
            ).fetchone()
            if row and "'promised'" not in row["sql"]:
                self._db.executescript(
                    """
                    ALTER TABLE nudge_follow_ups RENAME TO nudge_follow_ups_old;
                    CREATE TABLE nudge_follow_ups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('hours', 'day', 'last', 'order_incomplete', 'promised', 'awaiting_payment')),
                        due_at REAL NOT NULL,
                        sent_at REAL
                    );
                    INSERT INTO nudge_follow_ups (id, chat_id, kind, due_at, sent_at)
                        SELECT id, chat_id, kind, due_at, sent_at FROM nudge_follow_ups_old;
                    DROP TABLE nudge_follow_ups_old;
                    CREATE INDEX IF NOT EXISTS nudge_follow_ups_pending_idx
                        ON nudge_follow_ups(chat_id, sent_at);
                    """
                )
                self._db.commit()

    def is_paused(self, chat_id: int) -> bool:
        """Оператор пишет клиенту вручную из CRM — бот в этот чат не отвечает,
        пока не включат обратно."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM paused_chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row is not None

    def pause_chat(self, chat_id: int, now: float, paused_by: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO paused_chats(chat_id, paused_at, paused_by) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    paused_at = excluded.paused_at,
                    paused_by = excluded.paused_by
                """,
                (chat_id, now, paused_by),
            )
            self._db.commit()

    def resume_chat(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM paused_chats WHERE chat_id = ?", (chat_id,))
            self._db.commit()

    def log_event(
        self,
        chat_id: int | None,
        level: str,
        stage: str,
        event: str,
        message: str = "",
        details: dict | None = None,
    ) -> None:
        """Живой журнал того, что делает бот — читает CRM (живой стрим на
        странице настроек). Сбой записи лога не должен ронять сам бот."""
        try:
            with self._lock:
                self._db.execute(
                    """
                    INSERT INTO bot_events (chat_id, level, stage, event, message, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, level, stage, event, message, json.dumps(details) if details else None, time.time()),
                )
                self._db.commit()
        except Exception:
            pass

    def list_events(self, *, level: str | None = None, since_id: int = 0, limit: int = 150) -> list[dict]:
        safe_limit = max(1, min(limit, 500))
        with self._lock:
            if level:
                rows = self._db.execute(
                    "SELECT * FROM bot_events WHERE level = ? AND id > ? ORDER BY id DESC LIMIT ?",
                    (level, since_id, safe_limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM bot_events WHERE id > ? ORDER BY id DESC LIMIT ?",
                    (since_id, safe_limit),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "chatId": row["chat_id"],
                "level": row["level"],
                "stage": row["stage"],
                "event": row["event"],
                "message": row["message"],
                "details": json.loads(row["details"]) if row["details"] else None,
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def get_bot_setting(self, key: str) -> str | None:
        """Сохранённый override настройки бота или None, если его нет.

        Пустая строка не хранится как override: saved_setting("") ведёт себя
        как отсутствие override, чтобы промпт никогда не стал пустым.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_bot_setting(self, key: str, value: str) -> None:
        now = time.time()
        with self._lock:
            if value:
                self._db.execute(
                    """
                    INSERT INTO bot_settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )
            else:
                self._db.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
            self._db.commit()

    def claim_message(self, chat_id: int, message_id: int) -> bool:
        """Atomically claim a Telegram message. False means it is a duplicate."""
        with self._lock:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO processed_messages(chat_id, message_id, created_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, message_id, time.time()),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def allow_request(
        self, chat_id: int, limit: int, window_seconds: int = 60
    ) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            self._db.execute("DELETE FROM rate_events WHERE created_at < ?", (cutoff,))
            count = self._db.execute(
                "SELECT COUNT(*) FROM rate_events WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]
            if count >= limit:
                self._db.commit()
                return False
            self._db.execute(
                "INSERT INTO rate_events(chat_id, created_at) VALUES (?, ?)",
                (chat_id, now),
            )
            self._db.commit()
            return True

    def claim_event(self, chat_id: int, event: str) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO sent_events(chat_id, event, created_at) VALUES (?, ?, ?)",
                (chat_id, event, time.time()),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def has_event(self, chat_id: int, event: str) -> bool:
        """Как claim_event, но не потребляет событие — только проверяет,
        было ли оно уже отправлено этому чату."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM sent_events WHERE chat_id = ? AND event = ? LIMIT 1",
                (chat_id, event),
            ).fetchone()
        return row is not None

    def has_message_after(self, chat_id: int, message_id: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id > ? LIMIT 1",
                (chat_id, message_id),
            ).fetchone()
        return row is not None

    def history(self, chat_id: int, limit: int = 20) -> list[ChatMessage]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content
                    FROM messages
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id
                """,
                (chat_id, limit),
            ).fetchall()
        return [ChatMessage(role=row["role"], content=row["content"]) for row in rows]

    def add_exchange(
        self,
        chat_id: int,
        message_id: int,
        user_text: str,
        assistant_text: str,
        kind: str,
    ) -> None:
        now = time.time()
        with self._lock:
            self._db.executemany(
                """
                INSERT OR IGNORE INTO messages(
                    chat_id, telegram_message_id, role, content, kind, created_at, sender
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (chat_id, message_id, "user", user_text, kind, now, "client"),
                    (chat_id, message_id, "assistant", assistant_text, "text", now, "bot"),
                ],
            )
            self._db.commit()

    def add_incoming_message(self, chat_id: int, message_id: int, text: str, kind: str) -> None:
        """Чат на паузе (оператор ведёт клиента вручную) — сохраняем входящее
        сообщение клиента без ответа бота, чтобы оператор видел его в CRM."""
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT OR IGNORE INTO messages(
                    chat_id, telegram_message_id, role, content, kind, created_at, sender
                ) VALUES (?, ?, 'user', ?, ?, ?, 'client')
                """,
                (chat_id, message_id, text, kind, now),
            )
            self._db.commit()

    def add_operator_message(
        self, chat_id: int, message_id: int, text: str, sender: str = "bot"
    ) -> None:
        """Исходящее без парного входящего (проактивные отправки бота и
        ручные сообщения оператора из CRM). role остаётся 'assistant' —
        для истории ИИ; кто реально написал, различает sender."""
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT OR IGNORE INTO messages(
                    chat_id, telegram_message_id, role, content, kind, created_at, sender
                ) VALUES (?, ?, 'assistant', ?, 'text', ?, ?)
                """,
                (chat_id, message_id, text, now, sender),
            )
            self._db.commit()

    def list_chats(self, limit: int = 100) -> list[dict]:
        """Чаты для раздела «Диалоги» в CRM: последнее сообщение, время,
        статус паузы."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT m.chat_id,
                       MAX(m.created_at) AS last_at,
                       (SELECT content FROM messages WHERE chat_id = m.chat_id ORDER BY id DESC LIMIT 1) AS last_text,
                       (SELECT sender FROM messages WHERE chat_id = m.chat_id ORDER BY id DESC LIMIT 1) AS last_sender,
                       EXISTS(SELECT 1 FROM paused_chats p WHERE p.chat_id = m.chat_id) AS paused
                  FROM messages m
                 GROUP BY m.chat_id
                 ORDER BY last_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "chatId": row["chat_id"],
                "lastAt": row["last_at"],
                "lastText": row["last_text"],
                "lastSender": row["last_sender"],
                "paused": bool(row["paused"]),
            }
            for row in rows
        ]

    def chat_messages(self, chat_id: int, limit: int = 300) -> list[dict]:
        """Переписка одного чата для CRM, старые -> новые."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, role, content, kind, created_at, sender
                  FROM messages
                 WHERE chat_id = ?
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "kind": row["kind"],
                "createdAt": row["created_at"],
                "sender": row["sender"] or ("client" if row["role"] == "user" else "bot"),
            }
            for row in reversed(rows)
        ]

    def memory(self, chat_id: int) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT summary FROM customer_memory WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row["summary"] if row else ""

    def update_memory(
        self, chat_id: int, customer_summary: str, order_summary: str
    ) -> None:
        now = time.time()
        with self._lock:
            if customer_summary.strip():
                self._db.execute(
                    """
                    INSERT INTO customer_memory(chat_id, summary, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        summary = excluded.summary,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, customer_summary.strip(), now),
                )
            if order_summary.strip():
                self._db.execute(
                    """
                    INSERT INTO orders(chat_id, summary, status, updated_at)
                    VALUES (?, ?, 'active', ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        summary = excluded.summary,
                        status = 'active',
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, order_summary.strip(), now),
                )
            self._db.commit()

    def clear_chat(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM customer_memory WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM orders WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM rate_events WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM sent_events WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM daily_nudges WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM nudge_follow_ups WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM pending_payments WHERE chat_id = ?", (chat_id,))
            self._db.commit()

    def schedule_daily_nudge(self, chat_id: int, next_due_at: float) -> None:
        """Заводит расписание один раз при первом контакте — время дня якорится
        на момент, когда клиент написал впервые. Повторный вызов не сдвигает
        уже существующее расписание."""
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO daily_nudges (chat_id, next_due_at, sent_count) VALUES (?, ?, 0)",
                (chat_id, next_due_at),
            )
            self._db.commit()

    def due_daily_nudges(self, now: float, max_sends: int, limit: int = 50) -> list[int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT chat_id FROM daily_nudges WHERE next_due_at <= ? AND sent_count < ? LIMIT ?",
                (now, max_sends, limit),
            ).fetchall()
        return [row["chat_id"] for row in rows]

    def advance_daily_nudge(self, chat_id: int, next_due_at: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE daily_nudges SET next_due_at = ?, sent_count = sent_count + 1 WHERE chat_id = ?",
                (next_due_at, chat_id),
            )
            self._db.commit()

    def cancel_daily_nudge(self, chat_id: int) -> None:
        """Клиент оформил заказ — больше не напоминаем про скидку."""
        with self._lock:
            self._db.execute("DELETE FROM daily_nudges WHERE chat_id = ?", (chat_id,))
            self._db.commit()

    def schedule_nudge_chain(self, chat_id: int, now: float, first_due_at: float | None = None) -> None:
        """Простой без ответа: сначала дожим (по умолчанию — через несколько
        часов, либо в момент first_due_at, если передан) → на след. день/после
        консультации → финальное сообщение. Отменяет любые прежние
        напоминания — они бы дублировали друг друга."""
        self.cancel_nudge_follow_ups(chat_id)
        with self._lock:
            self._db.executemany(
                "INSERT INTO nudge_follow_ups (chat_id, kind, due_at) VALUES (?, ?, ?)",
                [
                    (chat_id, "hours", first_due_at if first_due_at is not None else now + 3 * 3600),
                    (chat_id, "day", now + 27 * 3600),
                    (chat_id, "last", now + 75 * 3600),
                ],
            )
            self._db.commit()

    def schedule_order_incomplete_nudge(self, chat_id: int, now: float) -> None:
        """Клиент сказал «беру»/«оформляйте», но не довёл заказ до конца —
        одно напоминание вместо обычной цепочки простоя."""
        self.cancel_nudge_follow_ups(chat_id)
        with self._lock:
            self._db.execute(
                "INSERT INTO nudge_follow_ups (chat_id, kind, due_at) VALUES (?, 'order_incomplete', ?)",
                (chat_id, now + 3 * 3600),
            )
            self._db.commit()

    def schedule_promised_follow_up(self, chat_id: int, due_at: float) -> None:
        """Клиент сам назвал момент, когда вернётся («завтра зп — закажу») —
        напоминание ставится точно на этот момент вместо обычной цепочки."""
        self.cancel_nudge_follow_ups(chat_id)
        with self._lock:
            self._db.execute(
                "INSERT INTO nudge_follow_ups (chat_id, kind, due_at) VALUES (?, 'promised', ?)",
                (chat_id, due_at),
            )
            self._db.commit()

    def schedule_awaiting_payment_reminder(self, chat_id: int, due_at: float) -> None:
        """QR отправлен, чек ещё не пришёл — бот сам переспросит об оплате,
        не присылая QR повторно."""
        self.cancel_nudge_follow_ups(chat_id)
        with self._lock:
            self._db.execute(
                "INSERT INTO nudge_follow_ups (chat_id, kind, due_at) VALUES (?, 'awaiting_payment', ?)",
                (chat_id, due_at),
            )
            self._db.commit()

    def cancel_nudge_follow_ups(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM nudge_follow_ups WHERE chat_id = ? AND sent_at IS NULL",
                (chat_id,),
            )
            self._db.commit()

    def due_nudge_follow_ups(self, now: float, limit: int = 50) -> list[tuple[int, int, str]]:
        """Атомарно захватывает просроченные напоминания и возвращает
        (id, chat_id, kind) — вызывающий код сам решает, что отправить."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, chat_id, kind FROM nudge_follow_ups WHERE sent_at IS NULL AND due_at <= ? LIMIT ?",
                (now, limit),
            ).fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                self._db.execute(
                    f"UPDATE nudge_follow_ups SET sent_at = ? WHERE id IN ({placeholders})",
                    (now, *ids),
                )
                self._db.commit()
        return [(row["id"], row["chat_id"], row["kind"]) for row in rows]

    def incoming_message_count(self, chat_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE chat_id = ? AND role = 'user'",
                (chat_id,),
            ).fetchone()
        return row["count"] if row else 0

    def save_pending_payment(
        self,
        chat_id: int,
        *,
        order_summary: str,
        amount: float | None,
        currency: str | None,
        customer_name: str | None,
        customer_phone: str | None,
        city: str | None,
        address: str | None,
        customer_username: str | None,
        source: str | None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO pending_payments
                    (chat_id, order_summary, amount, currency, customer_name, customer_phone, city, address, customer_username, source, reminder_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    order_summary = excluded.order_summary,
                    amount = excluded.amount,
                    currency = excluded.currency,
                    customer_name = excluded.customer_name,
                    customer_phone = excluded.customer_phone,
                    city = excluded.city,
                    address = excluded.address,
                    customer_username = excluded.customer_username,
                    source = excluded.source,
                    receipt_verified = 0,
                    receipt_file_id = NULL,
                    receipt_kind = NULL,
                    reminder_count = 0,
                    created_at = excluded.created_at
                """,
                (chat_id, order_summary, amount, currency, customer_name, customer_phone, city, address, customer_username, source, time.time()),
            )
            self._db.commit()

    def mark_pending_payment_receipt(self, chat_id: int, file_id: str, kind: str) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE pending_payments SET
                    receipt_verified = 1,
                    receipt_file_id = ?,
                    receipt_kind = ?
                WHERE chat_id = ?
                """,
                (file_id, kind, chat_id),
            )
            self._db.commit()

    def update_pending_payment_details(
        self,
        chat_id: int,
        *,
        order_summary: str | None,
        customer_name: str | None,
        customer_phone: str | None,
        city: str | None,
        address: str | None,
    ) -> None:
        """Дополняет заказ данными, которые клиент прислал после получения QR."""
        with self._lock:
            self._db.execute(
                """
                UPDATE pending_payments SET
                    order_summary = COALESCE(NULLIF(?, ''), order_summary),
                    customer_name = COALESCE(NULLIF(?, ''), customer_name),
                    customer_phone = COALESCE(NULLIF(?, ''), customer_phone),
                    city = COALESCE(NULLIF(?, ''), city),
                    address = COALESCE(NULLIF(?, ''), address)
                WHERE chat_id = ?
                """,
                (order_summary, customer_name, customer_phone, city, address, chat_id),
            )
            self._db.commit()

    def get_pending_payment(self, chat_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pending_payments WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return dict(row) if row else None

    def increment_payment_reminder(self, chat_id: int) -> int:
        with self._lock:
            self._db.execute(
                "UPDATE pending_payments SET reminder_count = reminder_count + 1 WHERE chat_id = ?",
                (chat_id,),
            )
            self._db.commit()
            row = self._db.execute(
                "SELECT reminder_count FROM pending_payments WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row["reminder_count"] if row else 0

    def clear_pending_payment(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending_payments WHERE chat_id = ?", (chat_id,))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
