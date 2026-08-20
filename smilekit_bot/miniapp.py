"""Telegram Mini App «Моя программа SmileKit»: авторизация, курс, API.

Живёт на том же aiohttp-приложении, что и /internal/* (см. api.py), но под
своим префиксом /app и со своей авторизацией: не общий сервисный токен CRM, а
подпись initData самого Telegram. Продающий бот при этом не меняется — Mini App
это второй, сервисный контур того же бота: бот продаёт коробку, приложение
ведёт клиента по курсу.

Проверка initData — по документации Telegram Web Apps: секретный ключ это
HMAC-SHA256 от токена бота с ключом "WebAppData", а подпись данных — HMAC-SHA256
от отсортированной строки "key=value", склеенной переводами строк.
"""

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from .miniapp_store import (
    COURSE_DAYS,
    PHOTO_DAYS,
    PROCEDURE_DAYS,
    PROCEDURE_MINUTES,
    REWARD_PERCENT,
    REWARD_PROCEDURES_REQUIRED,
    MiniAppStore,
    bishkek_day_start,
)

LOGGER = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
INIT_DATA_HEADER = "X-Telegram-Init-Data"
INIT_DATA_MAX_AGE_SECONDS = 24 * 3600
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_ASK_LENGTH = 500
# Академия целиком отдаётся фронтендом (content.js) — сервер знает только,
# сколько статей всего, чтобы считать Smile Score одинаково с приложением.
ACADEMY_SCORE_TARGET = 10
SUPPORT_TOPICS = {
    "sensitivity": "Появилась чувствительность",
    "no_result": "Не вижу результат",
    "package": "Проблема с набором",
    "how_to": "Не знаю, как использовать",
    "other": "Другой вопрос",
}


# ---------------------------------------------------------------- авторизация


def validate_init_data(init_data: str, bot_token: str, *, now: float | None = None) -> dict | None:
    """Разобранный initData, если подпись Telegram сходится, иначе None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    # signature — подпись Telegram для сторонней валидации по Ed25519, в
    # data_check_string она не входит и ломает HMAC, если её не убрать.
    pairs.pop("signature", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = float(pairs.get("auth_date", 0))
    except ValueError:
        return None
    if (now or time.time()) - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    if not user.get("id"):
        return None
    return {"user": user, "auth_date": auth_date}


def _auth(request: web.Request) -> dict | None:
    init_data = request.headers.get(INIT_DATA_HEADER, "")
    token = request.app["settings"].telegram_bot_token.get_secret_value()
    return validate_init_data(init_data, token)


def _unauthorized() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=401)


# --------------------------------------------------------------------- курс


def current_day(user: dict, now: float) -> int:
    """Номер дня курса (1..14). 0 — курс ещё не начат (нет онбординга)."""
    started = user.get("course_started_at")
    if not started:
        return 0
    passed = int((bishkek_day_start(now) - started) // 86400)
    return max(1, min(COURSE_DAYS, passed + 1))


def day_plan(day: int, user: dict) -> list[dict]:
    """Задачи дня. Порядок и тексты одни и те же для приложения и для
    напоминаний бота — иначе клиент видел бы в чате одно, а в приложении другое."""
    if day < 1 or day > COURSE_DAYS:
        return []
    drink = (user or {}).get("drink")
    sensitivity = (user or {}).get("sensitivity")
    tasks: list[dict] = []
    if day in PROCEDURE_DAYS:
        tasks.append(
            {
                "id": "procedure",
                "icon": "🦷",
                "text": f"Использовать SmileKit — {PROCEDURE_MINUTES} минут",
                "hint": "Высуши зубы, наклей две полоски и держи рот приоткрытым",
            }
        )
        if drink == "coffee":
            hint = "Кофе — самый сильный краситель, дай эмали закрыться"
        elif drink == "tea":
            hint = "Чай красит не слабее кофе, лучше переждать"
        elif drink == "soda":
            hint = "Газировка и соки с красителями — тоже в этот список"
        else:
            hint = "Вода и белая еда в эти два часа — идеальный вариант"
        tasks.append(
            {
                "id": "no_staining",
                "icon": "💧",
                "text": "Не пить красящие напитки 2 часа",
                "hint": hint,
            }
        )
    else:
        tasks.append(
            {
                "id": "care",
                "icon": "🪥",
                "text": "Мягкая чистка зубов утром и вечером",
                "hint": "День без полосок — день, когда эмаль спокойно закрепляет результат",
            }
        )
        tasks.append(
            {
                "id": "water",
                "icon": "💧",
                "text": "Прополоскать рот водой после кофе или чая",
                "hint": "Простая вода сразу после чашки заметно снижает окрашивание",
            }
        )
    if day in PHOTO_DAYS:
        label = {1: "Сделать фото «до»", 7: "Сделать контрольное фото", 14: "Сделать фото «после»"}[day]
        tasks.append(
            {
                "id": "photo",
                "icon": "✨",
                "text": label,
                "hint": "Один и тот же свет и ракурс — тогда сравнение будет честным",
            }
        )
    if sensitivity in {"yes", "sometimes"} and day in PROCEDURE_DAYS:
        tasks.append(
            {
                "id": "sensitivity",
                "icon": "🫧",
                "text": "Проверить ощущения после процедуры",
                "hint": "Есть заметный дискомфорт — сделай паузу на день, это нормально",
            }
        )
    return tasks


def streak(check_ins: dict[int, dict], day: int) -> int:
    """Серия подряд отмеченных дней, считая от сегодняшнего назад."""
    count = 0
    cursor = day
    while cursor >= 1 and cursor in check_ins:
        count += 1
        cursor -= 1
    return count


def smile_score(check_ins: dict[int, dict], photos: list[dict], reads: list[str], quiz: dict) -> int:
    """Игровой показатель соблюдения программы, 0–100. Не медицинский
    показатель: считает только то, что клиент реально сделал в приложении."""
    procedures = sum(1 for day in check_ins if day in PROCEDURE_DAYS)
    care = sum(1 for day in check_ins if day not in PROCEDURE_DAYS)
    score = procedures / len(PROCEDURE_DAYS) * 50
    score += min(care, COURSE_DAYS - len(PROCEDURE_DAYS)) / (COURSE_DAYS - len(PROCEDURE_DAYS)) * 15
    score += min(len(photos), len(PHOTO_DAYS)) / len(PHOTO_DAYS) * 15
    score += min(len(reads), ACADEMY_SCORE_TARGET) / ACADEMY_SCORE_TARGET * 10
    total = quiz.get("total") or 0
    if total:
        score += min(quiz.get("best", 0), total) / total * 10
    return round(score)


def reward_state(store: MiniAppStore, chat_id: int, check_ins: dict[int, dict]) -> dict:
    procedures = sum(1 for day in check_ins if day in PROCEDURE_DAYS)
    unlocked = procedures >= REWARD_PROCEDURES_REQUIRED
    reward = store.reward(chat_id)
    return {
        "unlocked": unlocked,
        "percent": REWARD_PERCENT,
        "procedures_done": procedures,
        "procedures_required": REWARD_PROCEDURES_REQUIRED,
        "left": max(0, REWARD_PROCEDURES_REQUIRED - procedures),
        "code": reward.get("code") if reward else None,
        "expires_at": reward.get("expires_at") if reward else None,
        "claimed": bool(reward.get("claimed_at")) if reward else False,
    }


def build_state(request: web.Request, auth: dict) -> dict:
    store: MiniAppStore = request.app["app_store"]
    tg_user = auth["user"]
    chat_id = int(tg_user["id"])
    user = store.ensure_user(
        chat_id, tg_user.get("first_name") or "", tg_user.get("username")
    )
    now = time.time()
    day = current_day(user, now)
    check_ins = store.check_ins(chat_id)
    photos = store.photos(chat_id)
    reads = store.reads(chat_id)
    quiz = store.quiz(chat_id)
    return {
        "user": {
            "chat_id": chat_id,
            "first_name": user.get("first_name") or tg_user.get("first_name") or "",
            "goal": user.get("goal"),
            "drink": user.get("drink"),
            "sensitivity": user.get("sensitivity"),
            "onboarded": bool(user.get("onboarded_at")),
            "reminders_on": bool(user.get("reminders_on", 1)),
            "reminder_hour": user.get("reminder_hour", 20),
        },
        "course": {
            "day": day,
            "total_days": COURSE_DAYS,
            "procedure_days": list(PROCEDURE_DAYS),
            "photo_days": list(PHOTO_DAYS),
            "procedure_minutes": PROCEDURE_MINUTES,
            "started_at": user.get("course_started_at"),
            "finished": day >= COURSE_DAYS and len(check_ins) > 0,
        },
        "today": {
            "day": day,
            "is_procedure_day": day in PROCEDURE_DAYS,
            "tasks": day_plan(day, user),
            "done": day in check_ins,
            "done_tasks": check_ins.get(day, {}).get("tasks", []),
        },
        "check_ins": {str(k): v for k, v in check_ins.items()},
        "photos": [
            {"id": p["id"], "day": p["day"], "url": f"/app/media/{p['id']}/{p['token']}.jpg"}
            for p in photos
        ],
        "reads": reads,
        "quiz": quiz,
        "streak": streak(check_ins, day),
        "score": smile_score(check_ins, photos, reads, quiz),
        "reward": reward_state(store, chat_id, check_ins),
        "stats": {
            "procedures_done": sum(1 for d in check_ins if d in PROCEDURE_DAYS),
            "procedures_total": len(PROCEDURE_DAYS),
            "days_done": len(check_ins),
            "articles_read": len(reads),
        },
    }


# ----------------------------------------------------------------- обработчики


async def get_state_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    return web.json_response(build_state(request, auth))


async def post_onboarding_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    goal = str(payload.get("goal", ""))
    drink = str(payload.get("drink", ""))
    sensitivity = str(payload.get("sensitivity", ""))
    if goal not in {"refresh", "lighter", "max"}:
        return web.json_response({"error": "bad_goal"}, status=400)
    if drink not in {"coffee", "tea", "soda", "none"}:
        return web.json_response({"error": "bad_drink"}, status=400)
    if sensitivity not in {"yes", "sometimes", "no"}:
        return web.json_response({"error": "bad_sensitivity"}, status=400)
    store: MiniAppStore = request.app["app_store"]
    chat_id = int(auth["user"]["id"])
    store.ensure_user(
        chat_id, auth["user"].get("first_name") or "", auth["user"].get("username")
    )
    store.save_onboarding(chat_id, goal=goal, drink=drink, sensitivity=sensitivity)
    schedule_next_reminder(request.app, chat_id)
    return web.json_response(build_state(request, auth))


async def post_checkin_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    store: MiniAppStore = request.app["app_store"]
    chat_id = int(auth["user"]["id"])
    user = store.get_user(chat_id)
    day = int(payload.get("day") or current_day(user, time.time()))
    if day < 1 or day > COURSE_DAYS:
        return web.json_response({"error": "bad_day"}, status=400)
    if payload.get("undo"):
        store.undo_check_in(chat_id, day)
    else:
        allowed = {task["id"] for task in day_plan(day, user)}
        tasks = [t for t in payload.get("tasks", []) if t in allowed]
        store.check_in(chat_id, day, tasks)
        # Отметился сам — вечернее напоминание в этот день уже незачем.
        schedule_next_reminder(request.app, chat_id)
    return web.json_response(build_state(request, auth))


async def post_photo_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    reader = await request.multipart()
    day: int | None = None
    data = b""
    async for part in reader:
        if part.name == "day":
            day = int((await part.text()).strip())
        elif part.name == "photo":
            while chunk := await part.read_chunk():
                data += chunk
                if len(data) > MAX_PHOTO_BYTES:
                    return web.json_response({"error": "too_large"}, status=413)
    if not data or day is None or day < 1 or day > COURSE_DAYS:
        return web.json_response({"error": "bad_request"}, status=400)
    store: MiniAppStore = request.app["app_store"]
    store.add_photo(int(auth["user"]["id"]), day, data)
    return web.json_response(build_state(request, auth))


async def delete_photo_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    day = int(request.match_info["day"])
    request.app["app_store"].delete_photo(int(auth["user"]["id"]), day)
    return web.json_response(build_state(request, auth))


async def get_photo_media_handler(request: web.Request) -> web.StreamResponse:
    """Отдаётся по неугадываемому токену в пути: <img> в WebView не умеет
    слать заголовок с initData, а перекладывать подпись в query-строку —
    значит светить её в логах и истории."""
    store: MiniAppStore = request.app["app_store"]
    try:
        photo_id = int(request.match_info["photo_id"])
    except ValueError:
        raise web.HTTPNotFound()
    path = store.photo_path(photo_id, request.match_info["token"])
    if path is None:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


async def post_read_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    article_id = str(payload.get("article_id", ""))[:80]
    if not article_id:
        return web.json_response({"error": "bad_request"}, status=400)
    request.app["app_store"].mark_read(int(auth["user"]["id"]), article_id)
    return web.json_response(build_state(request, auth))


async def post_quiz_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    score = max(0, int(payload.get("score", 0)))
    total = max(1, int(payload.get("total", 1)))
    request.app["app_store"].save_quiz(int(auth["user"]["id"]), min(score, total), total)
    return web.json_response(build_state(request, auth))


async def post_settings_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    store: MiniAppStore = request.app["app_store"]
    chat_id = int(auth["user"]["id"])
    enabled = payload.get("reminders_on")
    hour = payload.get("reminder_hour")
    store.update_reminders(
        chat_id,
        enabled=None if enabled is None else bool(enabled),
        hour=None if hour is None else int(hour),
    )
    if enabled is False:
        store.cancel_reminder(chat_id)
    else:
        schedule_next_reminder(request.app, chat_id)
    return web.json_response(build_state(request, auth))


async def post_restart_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    chat_id = int(auth["user"]["id"])
    request.app["app_store"].restart_course(chat_id)
    schedule_next_reminder(request.app, chat_id)
    return web.json_response(build_state(request, auth))


async def post_reward_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    store: MiniAppStore = request.app["app_store"]
    chat_id = int(auth["user"]["id"])
    check_ins = store.check_ins(chat_id)
    procedures = sum(1 for day in check_ins if day in PROCEDURE_DAYS)
    if procedures < REWARD_PROCEDURES_REQUIRED:
        return web.json_response({"error": "locked"}, status=403)
    reward = store.issue_reward(chat_id)
    store.mark_reward_claimed(chat_id)
    bot = request.app.get("bot")
    if bot is not None:
        try:
            await bot.send_message(
                chat_id,
                "🎉 Курс SmileKit пройден!\n\n"
                f"Твоя награда — скидка {REWARD_PERCENT}% на следующий набор.\n"
                f"Промокод: {reward['code']}\n\n"
                "Назови его в этом чате, когда захочешь оформить новый заказ.",
            )
        except Exception:
            LOGGER.exception("Failed to deliver reward message chat=%s", chat_id)
    return web.json_response(build_state(request, auth))


async def post_support_handler(request: web.Request) -> web.Response:
    """Обращение из приложения падает обычным сообщением в тот же чат с ботом:
    менеджер видит его в CRM вместе с контекстом дня курса, отдельный канал
    поддержки для этого заводить не нужно."""
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    topic = str(payload.get("topic", "other"))
    text = str(payload.get("text", "")).strip()[:1000]
    if topic not in SUPPORT_TOPICS:
        return web.json_response({"error": "bad_topic"}, status=400)
    store: MiniAppStore = request.app["app_store"]
    chat_id = int(auth["user"]["id"])
    user = store.get_user(chat_id)
    day = current_day(user, time.time())
    check_ins = store.check_ins(chat_id)
    procedures = sum(1 for d in check_ins if d in PROCEDURE_DAYS)
    summary = (
        f"Обращение из приложения\n"
        f"Тема: {SUPPORT_TOPICS[topic]}\n"
        f"День курса: {day} из {COURSE_DAYS}\n"
        f"Процедур выполнено: {procedures} из {len(PROCEDURE_DAYS)}"
    )
    if text:
        summary += f"\n\n{text}"
    bot = request.app.get("bot")
    if bot is not None:
        try:
            await bot.send_message(chat_id, summary)
            await bot.send_message(
                chat_id,
                "Передал(а) обращение менеджеру — ответим здесь, в этом чате 💜",
            )
        except Exception:
            LOGGER.exception("Failed to deliver support ticket chat=%s", chat_id)
            return web.json_response({"error": "delivery_failed"}, status=502)
    request.app["storage"].log_event(
        chat_id, "info", "miniapp", "miniapp.support",
        "Обращение из Mini App", {"topic": topic, "day": day},
    )
    return web.json_response({"ok": True})


async def get_ask_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    history = request.app["app_store"].ask_history(int(auth["user"]["id"]))
    return web.json_response({"messages": history})


async def post_ask_handler(request: web.Request) -> web.Response:
    """ИИ-консультант приложения. Отвечает только про зубы, улыбку, уход и
    SmileKit: тему держит промпт в service.dental_answer, а не фильтр здесь —
    отдельный фильтр по словам ловил бы «а можно с брекетами?» как оффтоп."""
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    payload = await request.json()
    question = str(payload.get("text", "")).strip()[:MAX_ASK_LENGTH]
    if not question:
        return web.json_response({"error": "empty"}, status=400)
    store: MiniAppStore = request.app["app_store"]
    chat_id = int(auth["user"]["id"])
    user = store.get_user(chat_id)
    history = store.ask_history(chat_id, limit=10)
    context = (
        f"День курса: {current_day(user, time.time())} из {COURSE_DAYS}. "
        f"Цель: {user.get('goal') or 'не указана'}. "
        f"Чаще пьёт: {user.get('drink') or 'не указано'}. "
        f"Чувствительность: {user.get('sensitivity') or 'не указана'}."
    )
    assistant = request.app["assistant"]
    try:
        answer = await assistant.dental_answer(question, history, context)
    except Exception:
        LOGGER.exception("Mini App ask failed chat=%s", chat_id)
        return web.json_response({"error": "ai_unavailable"}, status=503)
    store.add_ask_message(chat_id, "user", question)
    store.add_ask_message(chat_id, "assistant", answer)
    return web.json_response({"answer": answer})


async def delete_ask_handler(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        return _unauthorized()
    request.app["app_store"].clear_ask_history(int(auth["user"]["id"]))
    return web.json_response({"ok": True})


# --------------------------------------------------------------- напоминания


def next_reminder_due_at(user: dict, check_ins: dict[int, dict], now: float) -> float | None:
    """Ближайший момент вечернего напоминания. None — напоминать больше не о чем
    (курс закончился, выключены напоминания или клиент ещё не начал курс)."""
    if not user or not user.get("reminders_on", 1) or not user.get("course_started_at"):
        return None
    hour = int(user.get("reminder_hour", 20))
    day = current_day(user, now)
    if day >= COURSE_DAYS and day in check_ins:
        return None
    today_slot = bishkek_day_start(now) + hour * 3600
    # Сегодня уже отмечено или время прошло — целимся в завтра.
    if now >= today_slot or day in check_ins:
        return today_slot + 86400
    return today_slot


def schedule_next_reminder(app: web.Application, chat_id: int) -> None:
    store: MiniAppStore = app["app_store"]
    user = store.get_user(chat_id)
    check_ins = store.check_ins(chat_id)
    due_at = next_reminder_due_at(user, check_ins, time.time())
    if due_at is None:
        store.cancel_reminder(chat_id)
        return
    store.schedule_reminder(chat_id, due_at, current_day(user, time.time()))


# --------------------------------------------------------------------- роуты


async def get_index_handler(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        WEBAPP_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


def setup_miniapp(app: web.Application, database_path: str) -> MiniAppStore:
    store = MiniAppStore(database_path)
    app["app_store"] = store
    app.router.add_get("/app", get_index_handler)
    app.router.add_get("/app/", get_index_handler)
    app.router.add_get("/app/media/{photo_id}/{token}.jpg", get_photo_media_handler)
    app.router.add_get("/app/api/state", get_state_handler)
    app.router.add_post("/app/api/onboarding", post_onboarding_handler)
    app.router.add_post("/app/api/checkin", post_checkin_handler)
    app.router.add_post("/app/api/photo", post_photo_handler)
    app.router.add_delete("/app/api/photo/{day}", delete_photo_handler)
    app.router.add_post("/app/api/read", post_read_handler)
    app.router.add_post("/app/api/quiz", post_quiz_handler)
    app.router.add_post("/app/api/settings", post_settings_handler)
    app.router.add_post("/app/api/restart", post_restart_handler)
    app.router.add_post("/app/api/reward", post_reward_handler)
    app.router.add_post("/app/api/support", post_support_handler)
    app.router.add_get("/app/api/ask", get_ask_handler)
    app.router.add_post("/app/api/ask", post_ask_handler)
    app.router.add_delete("/app/api/ask", delete_ask_handler)
    # Статика идёт последней: любой /app/<файл> ищется на диске уже после того,
    # как разобраны конкретные ручки выше.
    app.router.add_static("/app/", WEBAPP_DIR, name="webapp")
    return store
