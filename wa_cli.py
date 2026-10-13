#!/usr/bin/env python3
"""Локальный чат с новой логикой WhatsApp-бота (пакет smilekit_bot/wa).

Ничего не отправляет наружу: нет Green API, нет CRM, нет рабочих групп.
Сообщения печатаются в консоль, заказы и карточки менеджеру пишутся в
локальный файл data/wa_local.db. Время условное — его двигает /wait.

Запуск:  .venv/bin/python wa_cli.py

Команды:
  /wait 30m | 1h | 90m   — подождать (сработают напоминания, если положены)
  /time 21:50            — поставить время суток по Бишкеку
  /image receipt 1490    — прислать чек на сумму; /image other — другое фото;
                           /image unreadable — чек, который не удалось проверить
  /voice                 — голосовое, которое не расшифровалось
  /quote текст           — следующее сообщение будет ответом-цитатой на этот текст
  /manager, /bot         — менеджер взял чат / вернул боту
  /state, /log           — память диалога и журнал решений
  /reset, /exit
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

DB_PATH = ROOT / "data" / "wa_local.db"


class LocalBrain(OpenAIBrain):
    """Картинку в консоли задаёт команда /image — сама проверка чека не вызывается."""

    next_image = ImageCheck(kind="other")

    def check_image(self, image: object) -> ImageCheck:
        return self.next_image


class LocalSink:
    def send_card(self, card: dict) -> bool:
        print(f"   [карточка менеджеру] {card}")
        return True


def show(out) -> bool:
    label = {"text": "Бот", "photo": "Бот [фото]", "voice": "Бот [голосовое]", "qr": "Бот [QR]"}[out.kind]
    print(f"{label}: {out.body}\n   ({out.agent}: {out.why})")
    return True


def main() -> None:
    brain = LocalBrain.from_env(os.environ)
    if brain is None:
        print("Нет ключа модели (DEEPSEEK_API_KEY или OPENAI_API_KEY) — без него бот не понимает реплики.")
        return
    store = Store(str(DB_PATH))
    engine = Engine(store, brain, LocalSink())
    now = datetime.now(R.BISHKEK).timestamp()
    chat = f"local-{int(now)}"
    counter, quoted = 0, ""
    print(f"Новая логика WhatsApp, локально. Чат {chat}. Команды — в начале файла.\n")

    while True:
        try:
            line = input(f"[{R.bishkek_iso(now)[11:]}] Клиент: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        counter += 1
        item = None
        if line == "/exit":
            break
        if line == "/reset":
            chat, quoted = f"local-{int(now)}-{counter}", ""
            print("— новый диалог —\n")
        elif line.startswith("/wait"):
            match = re.search(r"(\d+)\s*([mhмч])", line)
            if not match:
                print("пример: /wait 30m")
                continue
            total = int(match.group(1)) * (60 if match.group(2) in "mм" else 3600)
            # Шаг в минуту — как рабочий таймер: напоминание уходит в свой момент.
            for _ in range(total // 60):
                now += 60
                engine.dispatch(engine.tick(chat, now), show, now)
        elif line.startswith("/time"):
            hour, minute = map(int, line.split()[1].split(":"))
            now = datetime.fromtimestamp(now, R.BISHKEK).replace(hour=hour, minute=minute).timestamp()
        elif line.startswith("/image"):
            parts = line.split()
            kind = parts[1] if len(parts) > 1 else "other"
            amount = float(parts[2]) if len(parts) > 2 else None
            brain.next_image = ImageCheck(kind=kind, amount=amount, recipient_ok=True, date_ok=True)
            item = Incoming(id=f"c{counter}", kind="image")
        elif line == "/voice":
            item = Incoming(id=f"c{counter}", kind="voice")
        elif line.startswith("/quote"):
            quoted = line[6:].strip()
        elif line == "/manager":
            engine.operator_took_over(chat, now)
        elif line == "/bot":
            engine.release_to_bot(chat, now)
        elif line == "/state":
            print(store.load(chat).model_dump_json(indent=2, exclude={"seen_ids"}))
        elif line == "/log":
            for ts, agent, event, detail in store.journal(chat):
                print(f"   {R.bishkek_iso(ts)[11:]} {agent:10} {event}: {detail}")
        else:
            item, quoted = Incoming(id=f"c{counter}", text=line, quoted=quoted), ""
        if item:
            engine.dispatch(engine.handle(chat, [item], now), show, now)
        print()


if __name__ == "__main__":
    main()
