"""Обезличивание персональных данных перед отправкой в OpenAI.

Телефон/адрес/имя клиента заменяются на плейсхолдеры вида {{PHONE_1}}
перед тем, как текст уходит модели. Модель видит и может использовать
плейсхолдер (например, повторить его в ответе или в order.contact_phone),
но никогда не видит сами цифры/адрес — их подставляют обратно уже в коде
после ответа модели.
"""

import re

# Кыргызстанские номера: +996XXXXXXXXX или 0XXXXXXXXX (мобильный/городской).
_PHONE_RE = re.compile(
    r"(?<!\d)(\+996[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|0\d{2,3}[\s\-]?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2})(?!\d)"
)
# Улица/дом/микрорайон — конкретный адрес доставки, не название города.
_ADDRESS_RE = re.compile(
    r"(?:ул\.?|улица|мкр\.?|микрорайон|пер\.?|переулок|проспект|просп\.?)\s*"
    r"[^\n,;.!?]{2,60}",
    re.IGNORECASE,
)
_ADDRESS_LINE_RE = re.compile(
    r"(?im)^[^\n]*(?:ул\.?|улица|мкр\.?|микрорайон|пер\.?|переулок|проспект|просп\.?)"
    r"[^\n]*$"
)
# Клиент сам представляется — берём только имя после явного вводного слова,
# чтобы не путать с обычными капитализированными словами в начале фразы.
_NAME_RE = re.compile(
    r"(?:меня\s+зовут|зовут\s+меня|зовут)\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2})",
    re.IGNORECASE,
)

_KINDS = (("PHONE", _PHONE_RE), ("ADDRESS", _ADDRESS_RE), ("NAME", _NAME_RE))

_CITY_NAMES = (
    ("бишкек", "Бишкек"), ("токмок", "Токмок"), ("ош", "Ош"),
    ("джалал-абад", "Джалал-Абад"), ("джалалабад", "Джалал-Абад"),
    ("манас", "Манас"), ("каракол", "Каракол"), ("нарын", "Нарын"),
    ("талас", "Талас"), ("баткен", "Баткен"), ("алмат", "Алматы"),
    ("астан", "Астана"), ("ташкент", "Ташкент"), ("москв", "Москва"),
)


def extract_delivery_details(text: str, *history: str) -> tuple[str | None, str | None, str | None]:
    """Достаёт телефон/адрес из подписи чека и город из текущего диалога."""
    value = text or ""
    phone_match = _PHONE_RE.search(value)
    address_match = _ADDRESS_LINE_RE.search(value)
    phone = phone_match.group(0).strip() if phone_match else None
    address = address_match.group(0).strip(" \t,;.") if address_match else None
    combined = "\n".join((value, *history)).casefold()
    city = next((name for marker, name in _CITY_NAMES if marker in combined), None)
    return phone, address, city


def build_mapping(*texts: str) -> dict[str, str]:
    """Сканирует переданные тексты и назначает каждому уникальному найденному
    значению плейсхолдер {{KIND_N}}. Порядок важен: телефон и адрес — точные
    паттерны, имя проверяется последним и на уже "вычищенном" остатке текста,
    чтобы не задвоить совпадение внутри уже найденного номера/адреса."""
    mapping: dict[str, str] = {}
    counters = {"PHONE": 0, "ADDRESS": 0, "NAME": 0}
    working = "\n".join(t for t in texts if t)
    for kind, pattern in _KINDS:
        for match in pattern.finditer(working):
            value = match.group(1) if match.groups() else match.group(0)
            value = value.strip()
            if not value or value in mapping:
                continue
            counters[kind] += 1
            mapping[value] = f"{{{{{kind}_{counters[kind]}}}}}"
        working = pattern.sub(" ", working)
    return mapping


def apply_mapping(text: str, mapping: dict[str, str]) -> str:
    if not text or not mapping:
        return text
    redacted = text
    for value, placeholder in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        redacted = redacted.replace(value, placeholder)
    return redacted


def restore_mapping(text: str | None, mapping: dict[str, str]) -> str | None:
    if not text or not mapping:
        return text
    restored = text
    for value, placeholder in mapping.items():
        restored = restored.replace(placeholder, value)
    return restored
