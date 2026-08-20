"""Хранилище Telegram Mini App «Моя программа SmileKit».

Отдельный класс и отдельное соединение с той же базой, что и BotStorage:
таблицы приложения ничего не знают о продающем диалоге и наоборот, поэтому
смешивать их в одном классе незачем. SQLite в режиме WAL (его включает
BotStorage при старте) спокойно держит несколько соединений из одного процесса.

chat_id здесь — это Telegram user id: Mini App открывается только в личной
переписке, где chat_id == user.id, так что ключ совпадает с ключами бота и
менеджер видит клиента в CRM под тем же идентификатором.
"""

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

# Курс: 14 дней, 7 процедур. В коробке 14 полосок, за одно применение уходят
# две — значит применений ровно 7. Раскладываем их через день, промежуточные
# дни остаются днями ухода: так «14-дневная программа» совпадает с реальной
# коробкой, а не обещает клиенту процедуру, на которую не хватит полосок.
COURSE_DAYS = 14
PROCEDURE_DAYS = (1, 3, 5, 7, 9, 11, 13)
PROCEDURE_MINUTES = 30
PHOTO_DAYS = (1, 7, 14)
# Награда открывается, когда клиент реально прошёл курс, а не просто дожил до
# 14-го дня: 6 процедур из 7 — это ~85%.
REWARD_PROCEDURES_REQUIRED = 6
REWARD_PERCENT = 30
REWARD_TTL_SECONDS = 30 * 24 * 3600

BISHKEK_UTC_OFFSET_SECONDS = 6 * 3600


def bishkek_day_start(now: float) -> float:
    """Полночь по Бишкеку (UTC+6) для переданного момента, в UTC-секундах."""
    local = now + BISHKEK_UTC_OFFSET_SECONDS
    return local - (local % 86400) - BISHKEK_UTC_OFFSET_SECONDS


class MiniAppStore:
    def __init__(self, path: str) -> None:
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.media_dir = database.parent / "app_photos"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS app_users (
                    chat_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT,
                    goal TEXT,
                    drink TEXT,
                    sensitivity TEXT,
                    onboarded_at REAL,
                    course_started_at REAL,
                    reminder_hour INTEGER NOT NULL DEFAULT 20,
                    reminders_on INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_checkins (
                    chat_id INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    tasks TEXT NOT NULL DEFAULT '[]',
                    done_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, day)
                );
                CREATE TABLE IF NOT EXISTS app_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS app_photos_chat_idx
                    ON app_photos(chat_id, day);
                CREATE TABLE IF NOT EXISTS app_reads (
                    chat_id INTEGER NOT NULL,
                    article_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, article_id)
                );
                CREATE TABLE IF NOT EXISTS app_quiz (
                    chat_id INTEGER PRIMARY KEY,
                    best INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    plays INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_rewards (
                    chat_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    percent INTEGER NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    claimed_at REAL
                );
                CREATE TABLE IF NOT EXISTS app_reminders (
                    chat_id INTEGER PRIMARY KEY,
                    next_due_at REAL NOT NULL,
                    last_day INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS app_ask_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS app_ask_chat_idx
                    ON app_ask_messages(chat_id, id);
                """
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------- профиль ----------

    def ensure_user(self, chat_id: int, first_name: str, username: str | None) -> dict:
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO app_users (chat_id, first_name, username, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (chat_id, first_name, username, now, now),
            )
            self._db.commit()
        return self.get_user(chat_id)

    def get_user(self, chat_id: int) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM app_users WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return dict(row) if row else {}

    def save_onboarding(
        self, chat_id: int, *, goal: str, drink: str, sensitivity: str
    ) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                UPDATE app_users
                   SET goal = ?, drink = ?, sensitivity = ?,
                       onboarded_at = COALESCE(onboarded_at, ?),
                       course_started_at = COALESCE(course_started_at, ?),
                       updated_at = ?
                 WHERE chat_id = ?
                """,
                (goal, drink, sensitivity, now, bishkek_day_start(now), now, chat_id),
            )
            self._db.commit()

    def update_reminders(
        self, chat_id: int, *, enabled: bool | None = None, hour: int | None = None
    ) -> None:
        sets, params = [], []
        if enabled is not None:
            sets.append("reminders_on = ?")
            params.append(1 if enabled else 0)
        if hour is not None:
            sets.append("reminder_hour = ?")
            params.append(max(0, min(23, hour)))
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([time.time(), chat_id])
        with self._lock:
            self._db.execute(
                f"UPDATE app_users SET {', '.join(sets)} WHERE chat_id = ?", params
            )
            self._db.commit()

    def restart_course(self, chat_id: int) -> None:
        """Клиент открыл новую коробку: обнуляем отметки и фото, профиль и
        прочитанные статьи остаются — онбординг заново проходить незачем."""
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM app_checkins WHERE chat_id = ?", (chat_id,))
            self._db.execute("DELETE FROM app_photos WHERE chat_id = ?", (chat_id,))
            self._db.execute(
                "UPDATE app_users SET course_started_at = ?, updated_at = ? WHERE chat_id = ?",
                (bishkek_day_start(now), now, chat_id),
            )
            self._db.commit()

    # ---------- отметки дней ----------

    def check_in(self, chat_id: int, day: int, tasks: list[str]) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO app_checkins (chat_id, day, tasks, done_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, day) DO UPDATE SET
                    tasks = excluded.tasks, done_at = excluded.done_at
                """,
                (chat_id, day, json.dumps(tasks, ensure_ascii=False), time.time()),
            )
            self._db.commit()

    def undo_check_in(self, chat_id: int, day: int) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM app_checkins WHERE chat_id = ? AND day = ?", (chat_id, day)
            )
            self._db.commit()

    def check_ins(self, chat_id: int) -> dict[int, dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT day, tasks, done_at FROM app_checkins WHERE chat_id = ? ORDER BY day",
                (chat_id,),
            ).fetchall()
        return {
            row["day"]: {"tasks": json.loads(row["tasks"]), "done_at": row["done_at"]}
            for row in rows
        }

    # ---------- фото прогресса ----------

    def add_photo(self, chat_id: int, day: int, data: bytes) -> dict:
        token = secrets.token_urlsafe(16)
        filename = f"{chat_id}_{day}_{token}.jpg"
        (self.media_dir / filename).write_bytes(data)
        now = time.time()
        with self._lock:
            # Один снимок на день: повторное фото заменяет предыдущее, иначе
            # сравнение «до/после» пришлось бы объяснять клиенту выбором кадра.
            old = self._db.execute(
                "SELECT filename FROM app_photos WHERE chat_id = ? AND day = ?",
                (chat_id, day),
            ).fetchall()
            self._db.execute(
                "DELETE FROM app_photos WHERE chat_id = ? AND day = ?", (chat_id, day)
            )
            cursor = self._db.execute(
                "INSERT INTO app_photos (chat_id, day, token, filename, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (chat_id, day, token, filename, now),
            )
            self._db.commit()
        for row in old:
            (self.media_dir / row["filename"]).unlink(missing_ok=True)
        return {"id": cursor.lastrowid, "day": day, "token": token, "created_at": now}

    def photos(self, chat_id: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, day, token, created_at FROM app_photos"
                " WHERE chat_id = ? ORDER BY day",
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def photo_path(self, photo_id: int, token: str) -> Path | None:
        with self._lock:
            row = self._db.execute(
                "SELECT filename FROM app_photos WHERE id = ? AND token = ?",
                (photo_id, token),
            ).fetchone()
        if row is None:
            return None
        path = self.media_dir / row["filename"]
        return path if path.exists() else None

    def delete_photo(self, chat_id: int, day: int) -> None:
        with self._lock:
            rows = self._db.execute(
                "SELECT filename FROM app_photos WHERE chat_id = ? AND day = ?",
                (chat_id, day),
            ).fetchall()
            self._db.execute(
                "DELETE FROM app_photos WHERE chat_id = ? AND day = ?", (chat_id, day)
            )
            self._db.commit()
        for row in rows:
            (self.media_dir / row["filename"]).unlink(missing_ok=True)

    # ---------- академия и квиз ----------

    def mark_read(self, chat_id: int, article_id: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO app_reads (chat_id, article_id, created_at)"
                " VALUES (?, ?, ?)",
                (chat_id, article_id, time.time()),
            )
            self._db.commit()

    def reads(self, chat_id: int) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT article_id FROM app_reads WHERE chat_id = ?", (chat_id,)
            ).fetchall()
        return [row["article_id"] for row in rows]

    def save_quiz(self, chat_id: int, score: int, total: int) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO app_quiz (chat_id, best, total, plays, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    best = MAX(app_quiz.best, excluded.best),
                    total = excluded.total,
                    plays = app_quiz.plays + 1,
                    updated_at = excluded.updated_at
                """,
                (chat_id, score, total, time.time()),
            )
            self._db.commit()

    def quiz(self, chat_id: int) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT best, total, plays FROM app_quiz WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return dict(row) if row else {"best": 0, "total": 0, "plays": 0}

    # ---------- награда ----------

    def issue_reward(self, chat_id: int) -> dict:
        """Идемпотентно: повторный вызов возвращает уже выданный промокод,
        иначе клиент получал бы новый код при каждом открытии экрана."""
        existing = self.reward(chat_id)
        if existing:
            return existing
        now = time.time()
        code = f"SMILE{secrets.randbelow(9000) + 1000}"
        with self._lock:
            self._db.execute(
                "INSERT INTO app_rewards (chat_id, code, percent, issued_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (chat_id, code, REWARD_PERCENT, now, now + REWARD_TTL_SECONDS),
            )
            self._db.commit()
        return self.reward(chat_id)

    def reward(self, chat_id: int) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM app_rewards WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return dict(row) if row else {}

    def mark_reward_claimed(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE app_rewards SET claimed_at = ? WHERE chat_id = ? AND claimed_at IS NULL",
                (time.time(), chat_id),
            )
            self._db.commit()

    # ---------- напоминания ----------

    def schedule_reminder(self, chat_id: int, due_at: float, day: int) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO app_reminders (chat_id, next_due_at, last_day)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    next_due_at = excluded.next_due_at, last_day = excluded.last_day
                """,
                (chat_id, due_at, day),
            )
            self._db.commit()

    def due_reminders(self, now: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT r.chat_id, r.last_day, u.reminder_hour, u.reminders_on,
                       u.course_started_at, u.first_name, u.goal, u.drink, u.sensitivity
                  FROM app_reminders r
                  JOIN app_users u ON u.chat_id = r.chat_id
                 WHERE r.next_due_at <= ? AND u.reminders_on = 1
                """,
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_reminder(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM app_reminders WHERE chat_id = ?", (chat_id,))
            self._db.commit()

    # ---------- ИИ-чат «Спросить» ----------

    def add_ask_message(self, chat_id: int, role: str, content: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO app_ask_messages (chat_id, role, content, created_at)"
                " VALUES (?, ?, ?, ?)",
                (chat_id, role, content, time.time()),
            )
            self._db.commit()

    def ask_history(self, chat_id: int, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT role, content, created_at FROM app_ask_messages"
                " WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_ask_history(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM app_ask_messages WHERE chat_id = ?", (chat_id,))
            self._db.commit()
