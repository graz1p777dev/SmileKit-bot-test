import re
from datetime import datetime, timezone


MANAGER_CONTACT = "0999000845"
CONTACT_TEXT = (
    "Для оформления можно связаться с менеджером Smilekit: 0999000845."
)

_ORDER_INTENT = re.compile(
    r"\b("
    r"хочу\s+(?:заказать|купить|взять)|"
    r"(?:как|где)\s+(?:заказать|купить|оформить)|"
    r"(?:закажу|заказать|оформить(?:\s+заказ)?|оформляйте|оформляем|беру|возьму)|"
    r"можно\s+оформить|"
    r"заказ\s+(?:берейин|кылайын)|сатып\s+ал|алайын|"
    r"buyurtma|sotib\s+ol"
    r")\b",
    re.IGNORECASE,
)


def wants_to_order(text: str) -> bool:
    return bool(_ORDER_INTENT.search(text))


def ensure_manager_contact(message: str, response: str) -> str:
    if not wants_to_order(message):
        return response
    digits = re.sub(r"\D", "", response)
    if MANAGER_CONTACT in digits or MANAGER_CONTACT[-9:] in digits:
        return response
    return f"{response.rstrip()}\n\n{CONTACT_TEXT}"


_THINKING_OBJECTION = re.compile(
    r"(?:я\s+)?подума(?:ю|ем)|над(?:о|а)\s+подумать|дай(?:те)?\s+подумать|"
    r"мне\s+нужно\s+время|рассмотрю\s+ещ[её]",
    re.IGNORECASE,
)
_EXPENSIVE_OBJECTION = re.compile(
    r"дорог(?:о|овато|ая|ой)|не\s+потяну|не\s+укладываюсь\s+в\s+бюджет|"
    r"дешевле\s+есть|скидк[уи]\s+можно",
    re.IGNORECASE,
)


def classify_objection(text: str) -> str | None:
    """Явное возражение клиента — готовый ответ вместо генерации ИИ."""
    if _THINKING_OBJECTION.search(text):
        return "thinking"
    if _EXPENSIVE_OBJECTION.search(text):
        return "expensive"
    return None


_PROMISED_DAYS = re.compile(r"через\s+(\d+)\s*д(?:ень|ня|ней)\b", re.IGNORECASE)
_PROMISED_WEEK = re.compile(r"через\s+недел|на\s+следующей\s+недел", re.IGNORECASE)
_PROMISED_DAY_AFTER_TOMORROW = re.compile(r"послезавтра", re.IGNORECASE)
_PROMISED_TOMORROW = re.compile(r"завтра", re.IGNORECASE)


def promised_delay_hours(text: str) -> float | None:
    """Клиент сам назвал момент, когда вернётся («завтра зп — закажу») —
    напоминание ставится на этот момент вместо обычной цепочки простоя."""
    match = _PROMISED_DAYS.search(text)
    if match:
        return float(match.group(1)) * 24
    if _PROMISED_DAY_AFTER_TOMORROW.search(text):
        return 48.0
    if _PROMISED_TOMORROW.search(text):
        return 24.0
    if _PROMISED_WEEK.search(text):
        return 7 * 24.0
    return None


# Бишкек = UTC+6 — своя копия константы, чтобы sales.py не тянул зависимость
# от __main__.py ради одного числа.
_BISHKEK_UTC_OFFSET_SECONDS = 6 * 3600
_CLOCK_TIME = re.compile(
    r"(?:^|\s)(?:к|в|до)\s+(\d{1,2})(?:[:\.](\d{2}))?\s*"
    r"(вечера|утра|дня|ночи|часам?|часов)?",
    re.IGNORECASE,
)


def promised_clock_time_hours(text: str, now: float) -> float | None:
    """Клиент назвал конкретное время («напишу к 18:00») — часы до этого
    момента по Бишкеку, с переносом на завтра, если время уже прошло.

    Без минут и без слова-уточнения (вечера/утра/...) число слишком
    двусмысленно («в 2 раза», «в 5 моделях») — такие совпадения игнорируются.
    """
    match = _CLOCK_TIME.search(text)
    if not match:
        return None
    hour_str, minute_str, qualifier = match.groups()
    if minute_str is None and not qualifier:
        return None
    hour = int(hour_str)
    minute = int(minute_str) if minute_str is not None else 0
    if hour > 23 or minute > 59:
        return None
    qualifier = (qualifier or "").lower()
    if qualifier in ("вечера", "ночи") and hour < 12:
        hour += 12
    if hour > 23:
        return None
    shifted_now = now + _BISHKEK_UTC_OFFSET_SECONDS
    day_start = shifted_now - (shifted_now % 86400)
    target = day_start + hour * 3600 + minute * 60
    if target <= shifted_now:
        target += 86400
    target_utc = target - _BISHKEK_UTC_OFFSET_SECONDS
    return (target_utc - now) / 3600


_MONTH_STEMS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_DAY_OF_MONTH = re.compile(r"\b([1-9]|[12]\d|3[01])[-\s]?(?:го|ого)\b", re.IGNORECASE)
_EVENING_QUALIFIER = re.compile(r"вечер", re.IGNORECASE)


def _month_from_text(text: str) -> int | None:
    lowered = text.lower()
    for stem, number in _MONTH_STEMS.items():
        if stem in lowered:
            return number
    return None


def promised_calendar_date_hours(text: str, now: float) -> float | None:
    """Клиент назвал конкретное число месяца («после 5-го», «5 сентября»,
    «в сентябре 5-го») — часы до этого момента по Бишкеку.

    Время по умолчанию — 12:00, «вечером» — 18:00. Если месяц не назван и
    дата в текущем месяце уже прошла — переносим на следующий месяц; если
    месяц назван явно и дата в этом году уже прошла — на следующий год.
    """
    match = _DAY_OF_MONTH.search(text)
    if not match:
        return None
    day = int(match.group(1))
    month = _month_from_text(text)
    hour = 18 if _EVENING_QUALIFIER.search(text) else 12

    shifted_now = now + _BISHKEK_UTC_OFFSET_SECONDS
    now_dt = datetime.fromtimestamp(shifted_now, tz=timezone.utc)
    target_month = month or now_dt.month
    try:
        target = now_dt.replace(
            month=target_month, day=day, hour=hour, minute=0, second=0, microsecond=0
        )
    except ValueError:
        return None
    if target <= now_dt:
        if month is None:
            next_month = target_month + 1
            next_year = now_dt.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            try:
                target = target.replace(year=next_year, month=next_month)
            except ValueError:
                return None
        else:
            target = target.replace(year=now_dt.year + 1)
    target_utc = target.timestamp() - _BISHKEK_UTC_OFFSET_SECONDS
    return (target_utc - now) / 3600


_VAGUE_FUTURE_PROMISE = re.compile(
    r"через\s+(?:пару|несколько)\s+дн|после\s+зарплаты|"
    r"на\s+днях|как-нибудь\s+позже|чуть\s+позже\s+закажу",
    re.IGNORECASE,
)


def mentions_vague_future_promise(text: str) -> bool:
    """Клиент откладывает заказ без определимой даты («через пару дней»,
    «после зарплаты») — вместо того чтобы гадать интервал, бот должен
    уточнить конкретное число."""
    return bool(_VAGUE_FUTURE_PROMISE.search(text))


_PAYMENT_INFO_REQUEST = re.compile(
    r"реквизит|куда\s+(?:скинуть|перевести|переводить|оплатить|платить)|"
    r"номер\s+(?:карты|счёта|счета)|как\s+оплатить|способ\s+оплаты|"
    r"(?:можно|дайте|пришлите|скиньте|отправьте|нужен|нужно)\s+(?:мне\s+)?(?:qr|кр|ку[аэ]р)|"
    r"\bqr[\s-]?код|\bкр[\s-]?код",
    re.IGNORECASE,
)


def wants_payment_info(text: str) -> bool:
    """Клиент прямо спрашивает, куда/как платить — сразу шлём QR, а не
    номер менеджера через свободную генерацию."""
    return bool(_PAYMENT_INFO_REQUEST.search(text))


# Сомнение в честности магазина ДО покупки («вы не мошенники?», «это точно
# не обман?») — не жалоба: клиент ещё ничего не покупал, его надо успокоить,
# а не разбирать инцидент. Проверяется раньше is_complaint, потому что слова
# пересекаются («мошенник», «обман»).
_SCAM_DOUBT = re.compile(
    r"мошенн(?:ик|ики|ичество)|"
    r"(?:это\s+)?(?:точно\s+)?не\s+(?:обман|развод|скам|фейк)|"
    r"(?:а\s+)?вдруг\s+(?:вы\s+)?(?:обманете|кинете|развод|фейк)|"
    r"без\s+обмана|(?:это\s+)?(?:все|всё)?\s*реально\s*(?:да|\?)|"
    r"не\s+кин[её]те|не\s+обманете|вам\s+можно\s+доверять|"
    r"можно\s+(?:ли\s+)?вам\s+верить|вы\s+настоящие|это\s+надежно\s*\?",
    re.IGNORECASE,
)


def asks_scam_doubt(text: str) -> bool:
    return bool(_SCAM_DOUBT.search(text))


_COMPLAINT_MARKERS = re.compile(
    r"развод|обман(?:ули)?|кинул|мошенник|верните\s+деньги|хочу\s+возврат|"
    r"ужасн(?:о|ый|ая)|отвратительн|никогда\s+(?:больше\s+)?не\s+куплю|"
    r"плохо\s+сработал|не\s+помогл|это\s+не\s+то|разочарован",
    re.IGNORECASE,
)


def is_complaint(text: str) -> bool:
    """Явное недовольство — не додумываем причину, прямо спрашиваем её и
    сохраняем в CRM, чтобы менеджер разобрался."""
    return bool(_COMPLAINT_MARKERS.search(text))


_COOPERATION_MARKERS = re.compile(
    r"сотрудничеств|оптом|опт\b|дистрибьютор|дилер|партн[её]р|реселлер|"
    r"перепродаж|b2b|стать\s+представител|закупить?\s+большую\s+партию|"
    r"блогер|реклам(?:у|ировать)\s+ваш",
    re.IGNORECASE,
)

_GREETING_ONLY = re.compile(
    r"^\s*(?:здравствуйте|здравствуй|привет|добрый\s+(?:день|вечер|утро)|"
    r"салам|ассаламу\s+алейкум)[!,.?\s😊👋]*$",
    re.IGNORECASE,
)


def is_greeting_only(text: str) -> bool:
    return bool(_GREETING_ONLY.fullmatch(text))


def mentions_cooperation_inquiry(text: str) -> bool:
    """Опт/партнёрство/реклама — не обычный розничный покупатель, такой
    диалог не ведём по стандартному сценарию, а передаём человеку."""
    return bool(_COOPERATION_MARKERS.search(text))


_SHORT_AFFIRMATION = re.compile(
    r"^(?:(?:да|ага|угу|ок|окей|хорошо|давай|давайте|го|согласен|согласна)[!.,\s]*)+$",
    re.IGNORECASE,
)


def is_short_affirmation(text: str) -> bool:
    """Голое согласие («давайте», «да», «ок») — само по себе неоднозначно,
    имеет смысл только вместе с тем, что предлагал последний ответ бота."""
    return bool(_SHORT_AFFIRMATION.match(text.strip()))


_ORDER_CTA_MARKERS = ("оформить доставку", "оформить заказ", "оформить вам")


def confirms_order_offer(text: str, last_assistant_message: str) -> bool:
    """Клиент коротко согласился («давайте») именно на предложение оформить
    доставку/заказ — а не на что-то другое, что бот предложил последним
    (например «отправить ещё отзывы»)."""
    if not is_short_affirmation(text):
        return False
    lowered = (last_assistant_message or "").lower()
    return any(marker in lowered for marker in _ORDER_CTA_MARKERS)
