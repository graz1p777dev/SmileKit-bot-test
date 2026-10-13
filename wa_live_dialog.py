#!/usr/bin/env python3
"""Целые диалоги на настоящей модели: печатает, что бот ответил бы клиенту.

Запуск:  .venv/bin/python wa_live_dialog.py
Наружу ничего не уходит: память в оперативке, карточки менеджеру печатаются.
Строка вида «+30m» — клиент молчит столько времени; «[чек 1490]» — прислал чек.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from smilekit_bot.wa import rules as R
from smilekit_bot.wa.brain import ImageCheck, OpenAIBrain
from smilekit_bot.wa.engine import Engine, Incoming
from smilekit_bot.wa.memory import Store

DIALOGS = {
    "Обычная покупка": [
        "Здравствуйте",
        "из Оша",
        "Сколько полосок в коробке? На сколько применений хватает? Доставка платная?",
        "Да, давайте",
        "А сколько держать полоски?",
        "г. Ош, ул. Курманжан Датка 12, тел 0555 123 456",
        "[чек 400]",
    ],
    "Согласие не на заказ и условное согласие": [
        "Привет, цена?",
        "Бишкек",
        "А состав безопасный?",
        "Да",
        "Можно оплатить при получении?",
        "Можно купить половину коробки?",
        "Да, но сначала скажите, сколько держится результат",
        "Хорошо, я подумаю",
        "+30m",
        "+60m",
        "+120m",
    ],
    "Сразу готов купить, дорого, оплата при получении": [
        "Здравствуйте, я из Токмока, хочу заказать",
        "Не надо пока, дорого",
        "Ладно, беру, только оплата при получении",
        "Токмок, ул. Ленина 5",
        "0700111222",
    ],
    "География и специальные ветки": [
        "Здравствуйте, сколько стоит?",
        "Я сейчас в Алматы, но отправить нужно маме в село Арчалы",
        "А вы точно не мошенники?",
        "У вас есть сертификат соответствия?",
    ],
}


class DialogBrain(OpenAIBrain):
    image = ImageCheck(kind="other")

    def check_image(self, image: object) -> ImageCheck:
        return self.image


class PrintSink:
    def send_card(self, card: dict) -> bool:
        print(f"      [карточка менеджеру] {card}")
        return True


def show(out) -> bool:
    label = {"text": "", "photo": "[фото] ", "voice": "[голосовое] ", "qr": "[QR] "}[out.kind]
    print(f"   Бот: {label}{out.body}".replace("\n", "\n        "))
    print(f"      ({out.agent}: {out.why})")
    return True


def main() -> None:
    brain = DialogBrain.from_env(os.environ)
    if brain is None:
        print("Нет ключа модели (DEEPSEEK_API_KEY или OPENAI_API_KEY).")
        return
    print(f"Модель: {brain.model}")
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    for number, (title, lines) in enumerate(DIALOGS.items(), 1):
        if only and only != str(number):
            continue
        engine = Engine(Store(":memory:"), brain, PrintSink())
        chat, now = f"demo{number}", datetime(2026, 10, 12, 12, 0, tzinfo=R.BISHKEK).timestamp()
        print(f"\n═══ {number}. {title} ═══")
        for index, line in enumerate(lines):
            if line.startswith("+"):
                minutes = int(re.search(r"\d+", line).group())
                print(f"[{R.bishkek_iso(now)[11:]}] … клиент молчит {minutes} мин")
                for _ in range(minutes):
                    now += 60
                    engine.dispatch(engine.tick(chat, now), show, now)
                continue
            now += 60
            print(f"[{R.bishkek_iso(now)[11:]}] Клиент: {line}")
            receipt = re.match(r"\[чек (\d+)\]", line)
            if receipt:
                brain.image = ImageCheck(kind="receipt", amount=float(receipt.group(1)), recipient_ok=True, date_ok=True)
                item = Incoming(id=f"{chat}-{index}", kind="image")
            else:
                item = Incoming(id=f"{chat}-{index}", text=line)
            engine.dispatch(engine.handle(chat, [item], now), show, now)
        m = engine.store.load(chat)
        print(f"   Итог: этап {m.stage}, заказ {m.order.status}, цена {m.price.agreed or m.price.offered} {m.price.currency}, "
              f"предложений {m.sales.offers_made}, управление {m.control.mode}")


if __name__ == "__main__":
    main()
