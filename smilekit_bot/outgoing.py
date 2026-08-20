import re


MESSAGE_SEPARATOR = "<NEXT_MESSAGE>"
MAX_MESSAGE_PARTS = 3
AUTO_SPLIT_MIN_LENGTH = 180
# Единственный настоящий маркер в системе — MESSAGE_SEPARATOR. Модель иногда
# сама выдумывает другие теги вида <НЕ_ВИДНО_КЛИЕНТУ>...</НЕ_ВИДНО_КЛИЕНТУ>,
# пытаясь пометить кусок текста как внутренний — таких тегов код нигде не
# ждёт, и без этой защиты они уходят клиенту как есть (уже случалось на
# проде). Отрезаем сам тег вместе с содержимым: раз модель посчитала его
# "невидимым", он не должен долетать до клиента ни в каком виде.
_HIDDEN_TAG_BLOCK = re.compile(r"<(?!/?NEXT_MESSAGE\b)([A-ZА-ЯЁ_]+)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_STRAY_TAG = re.compile(r"</?(?!NEXT_MESSAGE\b)[A-ZА-ЯЁ_]+>", re.IGNORECASE)


def _strip_hallucinated_tags(text: str) -> str:
    without_blocks = _HIDDEN_TAG_BLOCK.sub("", text)
    return _STRAY_TAG.sub("", without_blocks)
_SAFETY_MARKERS = (
    "сильная боль",
    "жжение",
    "раздражение",
    "воспаление",
    "кровоточив",
    "обратиться к стоматологу",
    "прекратить применение",
)


def _split_long_explanation(text: str) -> list[str]:
    normalized = text.strip()
    lowered = normalized.lower()
    if len(normalized) < AUTO_SPLIT_MIN_LENGTH:
        return [text]
    if any(marker in lowered for marker in _SAFETY_MARKERS):
        return [text]
    if re.search(r"(?:^|\n)\s*\d+[.)]\s", normalized):
        return [text]

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", normalized)
        if part.strip()
    ]
    if len(sentences) < 3:
        return [text]
    return [sentences[0], " ".join(sentences[1:])]


def split_outgoing_messages(text: str) -> list[str]:
    """Remove the internal separator and return at most three client messages."""
    text = _strip_hallucinated_tags(text)
    if MESSAGE_SEPARATOR in text:
        raw_parts = text.split(MESSAGE_SEPARATOR)
    else:
        paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
        raw_parts = (
            paragraphs
            if 1 < len(paragraphs) <= MAX_MESSAGE_PARTS
            else _split_long_explanation(text)
        )
    parts = [part.strip() for part in raw_parts if part.strip()]
    if not parts:
        return [text.strip()] if text.strip() else []
    if len(parts) <= MAX_MESSAGE_PARTS:
        return parts
    return [
        *parts[: MAX_MESSAGE_PARTS - 1],
        "\n\n".join(parts[MAX_MESSAGE_PARTS - 1 :]),
    ]
