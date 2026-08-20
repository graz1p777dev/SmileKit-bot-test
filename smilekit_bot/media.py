import random
import re
from pathlib import Path

from aiogram.types import FSInputFile, InputMediaPhoto


RESULTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "results"
REVIEWS_DIR = Path(__file__).resolve().parent.parent / "assets" / "reviews"
REVIEW_PHOTOS_PER_REQUEST = 3

_PHOTO_REQUESTS = (
    "покажи фото",
    "показать фото",
    "есть фото",
    "фото результата",
    "фото результатов",
    "результаты клиентов",
    "результат клиентов",
    "до и после",
    "до/после",
    "сүрөт көрсөт",
    "жыйынтык сүрөт",
    "натыйжа сүрөт",
)

_REVIEW_REQUESTS = (
    "отзыв",
    "reviews",
)
# Голое согласие («давайте», «отправьте», «да») само по себе неоднозначно —
# засчитываем как просьбу прислать отзывы только если бот сам только что
# предложил их прислать (см. confirms_review_offer).
_REVIEW_CONFIRM = re.compile(
    r"^(?:ну\s+)?(?:да|ага|угу|ок|окей|хорошо|давай(?:те)?|"
    r"отправь(?:те)?|скинь(?:те)?|пришли(?:те)?|покажи(?:те)?|го)[!.,\s]*$",
    re.IGNORECASE,
)

_COMPLAINT_MARKERS = (
    "боль",
    "болит",
    "жжение",
    "раздраж",
    "воспален",
    "кров",
    "жалоб",
    "плохой результат",
    "не помог",
    "не работает",
)

_CITY_MARKERS = (
    "бишкек", "токмок", "ош", "джалал-абад", "джалалабад", "каракол", "нарын",
    "талас", "баткен", "манас", "алмат", "астан", "ташкент", "москв", "росси",
)
_PRICE_MARKERS = ("сколько стоит", "какая цена", "цена?", "1490 сом")


def wants_result_photos(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    if any(marker in normalized for marker in _COMPLAINT_MARKERS):
        return False
    return any(phrase in normalized for phrase in _PHOTO_REQUESTS)


def wants_review_photos(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    if any(marker in normalized for marker in _COMPLAINT_MARKERS):
        return False
    return any(phrase in normalized for phrase in _REVIEW_REQUESTS)


def confirms_review_offer(text: str, last_assistant_message: str) -> bool:
    """Клиент коротко согласился («давайте», «отправьте») именно на
    предложение прислать отзывы, которое бот только что сделал сам —
    без этого голое согласие можно перепутать с чем угодно другим."""
    if not _REVIEW_CONFIRM.match(text.strip()):
        return False
    lowered = (last_assistant_message or "").lower()
    return "отзыв" in lowered


def should_send_results_after_city(text: str, history: list[str]) -> bool:
    normalized = " ".join(text.lower().split())
    if any(marker in normalized for marker in _COMPLAINT_MARKERS):
        return False
    has_city = any(marker in normalized for marker in _CITY_MARKERS)
    prior_text = " ".join(item.lower() for item in history)
    prior_price = any(marker in prior_text for marker in _PRICE_MARKERS)
    asked_for_city = "из какого вы города" in prior_text or "вы в бишкеке" in prior_text
    short_location_answer = "?" not in normalized and len(normalized.split()) <= 5
    delivery_question = any(marker in normalized for marker in ("достав", "жеткир", "отправ", "привез"))
    # Частые города обрабатываются готовыми шаблонами выше, но неизвестный
    # город/страна не должны блокировать сценарий: ответ о доставке сформирует
    # GPT-5.6 Luna, а фото результатов всё равно отправятся после него.
    return prior_price and (
        has_city or delivery_question or (asked_for_city and short_location_answer)
    )


def result_photo_group() -> list[InputMediaPhoto]:
    return [
        InputMediaPhoto(
            media=FSInputFile(RESULTS_DIR / "before.jpg"),
        ),
        InputMediaPhoto(
            media=FSInputFile(RESULTS_DIR / "after.jpg"),
        ),
        InputMediaPhoto(
            media=FSInputFile(RESULTS_DIR / "resultotzyv.jpeg"),
        ),
    ]


def review_photo_group() -> list[InputMediaPhoto]:
    """Реальные карточки отзывов клиентов (с сайта smilekit) — каждый раз
    случайная подборка, чтобы не слать одно и то же при повторной просьбе."""
    files = sorted(REVIEWS_DIR.glob("*.jpg"))
    sample = random.sample(files, k=min(REVIEW_PHOTOS_PER_REQUEST, len(files)))
    return [InputMediaPhoto(media=FSInputFile(path)) for path in sample]
