#!/usr/bin/env python3
"""Проверка понимания на настоящей модели: фразы из задания → разбор диспетчера.

Запуск:  .venv/bin/python wa_live_check.py
Клиентам ничего не отправляется; тратит около 25 вызовов модели.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from smilekit_bot.wa.brain import OpenAIBrain

ORDER_OFFER = {"kind": "order", "text": "Оформить вам доставку? 😊"}
INFO_OFFER = {"kind": "composition", "text": "Состав безопасный. Могу прислать состав?"}
FACTS = "этап: consult\nязык клиента: ru\nдоставка: Ош (зона kg_other)\nцена 1490 сом"
NO_CITY = "этап: contact\nязык клиента: ru\nгород доставки неизвестен\nцена 1490 сом"

# (реплика, последнее предложение бота, факты, проверка, что ожидаем)
CASES = [
    ("Да", INFO_OFFER, FACTS, lambda u: u.intent != "order_now" and u.offer_reply == "accept", "согласие получить состав, не заказ"),
    ("Давайте", {"kind": "instruction", "text": "Отправить инструкцию?"}, FACTS, lambda u: u.intent != "order_now", "инструкция, не заказ"),
    ("Да, давайте", ORDER_OFFER, FACTS, lambda u: u.offer_reply == "accept", "согласие на оформление"),
    ("Да, но сначала скажите стоимость доставки", ORDER_OFFER, FACTS,
     lambda u: u.offer_reply == "conditional" and len(u.questions) == 1, "условное согласие + вопрос"),
    ("Можно оплатить при получении?", None, FACTS, lambda u: u.intent == "question" and not u.order_data.payment, "вопрос, не выбор оплаты"),
    ("Как оформить?", None, FACTS, lambda u: u.intent == "question", "вопрос о порядке"),
    ("Можно завтра заказать?", None, FACTS, lambda u: u.intent in ("order_later", "question"), "не заказ сейчас"),
    ("Хорошо, я подумаю", ORDER_OFFER, FACTS, lambda u: u.intent == "not_ready" and u.offer_reply == "decline_temporary", "временный отказ"),
    ("Не надо оформлять", ORDER_OFFER, FACTS, lambda u: u.intent in ("cancel_order", "not_ready", "stop_contact") and u.offer_reply != "accept", "отказ"),
    ("Беру одну коробку, оформляйте", None, FACTS, lambda u: u.intent == "order_now" and u.order_data.quantity == 1, "явное согласие, 1 шт."),
    ("Пришлите QR, я готов оплатить", None, FACTS, lambda u: u.intent == "order_now", "явное согласие"),
    ("Можно купить половину коробки?", None, FACTS, lambda u: u.intent == "question", "вопрос"),
    ("Сколько полосок? На сколько применений? Доставка платная?", None, FACTS, lambda u: len(u.questions) == 3, "три вопроса"),
    ("Спасибо, понятно", None, FACTS, lambda u: not u.questions and not u.geo.mentioned, "не вопрос и не город"),
    ("На сколько применений хватает коробки?", None, FACTS, lambda u: u.questions and u.questions[0].topic == "pack_contents", "комплектация"),
    ("Сколько держится результат?", None, FACTS, lambda u: u.questions and u.questions[0].topic == "result_duration", "длительность эффекта"),
    ("Сколько держать полоски?", None, FACTS, lambda u: u.questions and u.questions[0].topic == "usage_time", "время процедуры"),
    ("Здравствуйте, я из Оша, сколько стоит?", None, NO_CITY, lambda u: u.geo.zone == "kg_other" and len(u.questions) == 1, "город + вопрос"),
    ("в бишкеа", None, NO_CITY, lambda u: u.geo.zone == "bishkek", "опечатка в Бишкеке"),
    ("Токмоктон", None, NO_CITY, lambda u: u.geo.zone == "tokmok", "Токмок, кыргызская форма"),
    ("село Арчалы", None, NO_CITY, lambda u: u.geo.zone == "kg_other" or bool(u.geo.clarify), "село КР или уточнение"),
    ("Я сейчас в Алматы, но отправить нужно маме в Ош", None, NO_CITY, lambda u: u.geo.zone == "kg_other" and "Ош" in u.geo.place, "место доставки — Ош"),
    ("Берлин", None, NO_CITY, lambda u: u.geo.zone == "unsupported", "страна вне географии"),
    ("Давайте оформим", None, NO_CITY, lambda u: not u.geo.mentioned, "не название города"),
    ("Напишите завтра после обеда", None, FACTS, lambda u: u.callback_at.startswith("2026-10-13"), "просьба написать позже"),
    ("Доставьте завтра", None, FACTS, lambda u: not u.callback_at, "это не просьба написать позже"),
    ("После ваших полосок десны жжёт второй день", None, FACTS, lambda u: u.intent == "adverse_reaction", "нежелательная реакция"),
    ("А вы точно не мошенники?", None, FACTS, lambda u: u.intent == "question" and u.questions and u.questions[0].topic == "trust", "сомнение, не жалоба"),
]


def main() -> None:
    brain = OpenAIBrain.from_env(os.environ)
    if brain is None:
        print("Нет ключа модели (DEEPSEEK_API_KEY или OPENAI_API_KEY) — проверка не запускалась.")
        return
    print(f"Модель: {brain.model}\n")
    failed = 0
    for message, offer, facts, check, expect in CASES:
        try:
            u = brain.understand({
                "now": "2026-10-12 12:00", "facts": facts, "last_offer": offer,
                "quoted": "", "history": [], "message": message,
            })
            ok = bool(check(u))
            got = (f"intent={u.intent} offer_reply={u.offer_reply} zone={u.geo.zone}/{u.geo.place or '-'} "
                   f"clarify={u.geo.clarify or '-'} вопросы={[q.topic for q in u.questions]} callback={u.callback_at or '-'}")
        except Exception as error:
            ok, got = False, f"ошибка: {error}"
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} «{message}» — ждём: {expect}\n       {got}")
    print(f"\nИтого: {len(CASES) - failed} из {len(CASES)}")


if __name__ == "__main__":
    main()
