#!/usr/bin/env python3
"""Локальная страница для просмотра диалогов новой логики WhatsApp-бота.

Запуск:  .venv/bin/python wa_web.py   →  http://localhost:8765
Наружу ничего не уходит: память в оперативке, WhatsApp и CRM не подключены.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from aiohttp import web
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from smilekit_bot.wa import rules as R
from smilekit_bot.wa.brain import ImageCheck, OpenAIBrain
from smilekit_bot.wa.engine import Engine, Incoming
from smilekit_bot.wa.memory import Store
from wa_live_dialog import DIALOGS

PORT = int(os.environ.get("WA_WEB_PORT", "8765"))


class WebBrain(OpenAIBrain):
    image = ImageCheck(kind="other")

    def check_image(self, image: object) -> ImageCheck:
        return self.image


class Sink:
    """Карточки менеджеру показываются в чате отдельной плашкой."""

    def __init__(self) -> None:
        self.cards: list[dict] = []

    def send_card(self, card: dict) -> bool:
        self.cards.append(card)
        return True


class Lab:
    def __init__(self) -> None:
        self.brain = WebBrain.from_env(os.environ)
        self.ready = self.brain is not None
        self.sink = Sink()
        self.store = Store(":memory:")
        self.engine = Engine(self.store, self.brain, self.sink)
        self.clock: dict[str, float] = {}
        self.feed: dict[str, list[dict]] = {}
        self.counter = 0

    def now(self, chat: str) -> float:
        start = datetime.now(R.BISHKEK).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        return self.clock.setdefault(chat, start)

    def add(self, chat: str, who: str, kind: str, body: str, note: str = "") -> None:
        self.feed.setdefault(chat, []).append(
            {"who": who, "kind": kind, "body": body, "note": note, "time": R.bishkek_iso(self.now(chat))[11:]}
        )

    def _send(self, out) -> bool:
        self.add(out.chat_id, "bot", out.kind, out.body, f"{out.agent}: {out.why}")
        return True

    def _flush_cards(self, chat: str) -> None:
        for card in self.sink.cards:
            self.add(chat, "system", "card", card_text(card))
        self.sink.cards.clear()

    def incoming(self, chat: str, item: Incoming, shown: str, kind: str = "text") -> None:
        self.clock[chat] = self.now(chat) + 60
        self.add(chat, "client", kind, shown)
        now = self.now(chat)
        outs = self.engine.handle(chat, [item], now)
        self._flush_cards(chat)
        self.engine.dispatch(outs, self._send, now)

    def wait(self, chat: str, minutes: int) -> None:
        self.add(chat, "system", "wait", f"клиент молчит {minutes} мин")
        for _ in range(minutes):
            self.clock[chat] = self.now(chat) + 60
            now = self.now(chat)
            self.engine.dispatch(self.engine.tick(chat, now), self._send, now)
            self._flush_cards(chat)

    def next_id(self) -> str:
        self.counter += 1
        return f"w{self.counter}"


def card_text(card: dict) -> str:
    if card.get("type") == "handoff":
        return f"Передано менеджеру: {card['reason']}"
    pay = {"full": "полная оплата", "cod": "при получении", "prepay": "предоплата 400"}.get(card["payment"], card["payment"])
    receipt = {"check_ok": "прошёл автопроверку", "manual_review": f"на ручную проверку ({card['receipt_note']})", "none": "не нужен"}[card["receipt"]]
    qty = f"{card['quantity']} шт." + (" (не называл, по умолчанию)" if card["quantity_assumed"] else "")
    return (
        f"Заказ №{card.get('order_id')} → группа доставки\n{card['place']} · {qty} · {card['total']} {card['currency']} · {pay}\n"
        f"Чек: {receipt}\nАдрес: {card['address']}\nТелефон: {card['phone']}\nСогласие: «{card['consent']}»"
    )


lab = Lab()


async def page(request: web.Request) -> web.Response:
    return web.Response(text=(ROOT / "wa_web.html").read_text(encoding="utf-8"), content_type="text/html")


async def scenarios(request: web.Request) -> web.Response:
    return web.json_response({"ready": lab.ready, "scenarios": [{"title": t, "lines": l} for t, l in DIALOGS.items()]})


async def state(request: web.Request) -> web.Response:
    chat = request.query["chat"]
    m = lab.store.load(chat)
    summary = {
        "Этап": m.stage, "Кто ведёт": "менеджер" if m.control.mode == "manager" else "бот",
        "Город": f"{m.geo.place} ({m.geo.zone})" if m.geo.zone else "неизвестен",
        "Цена": f"{m.price.agreed or m.price.offered} {m.price.currency}" + (" (согласована)" if m.price.agreed else ""),
        "Ступень скидки": m.price.discount_level,
        "Отвечено вопросов в цикле": f"{m.sales.answered_in_cycle} из {R.QUESTIONS_PER_OFFER}",
        "Предложений оформить": m.sales.offers_made, "Временных отказов": m.sales.temporary_declines,
        "Последнее предложение": f"{m.sales.last_offer.kind}: {m.sales.last_offer.text}" if m.sales.last_offer else "—",
        "Заказ": m.order.status, "Оплата": m.order.payment or "—", "Чек": m.order.receipt,
        "Адрес": m.order.address or "—", "Телефон": m.order.phone or "—",
        "Напоминания": "остановлены" if m.wait.stopped or m.control.stop_sales else f"цепочка: {m.wait.chain}",
    }
    journal = [
        {"time": R.bishkek_iso(ts)[11:], "agent": agent, "event": event, "detail": detail}
        for ts, agent, event, detail in lab.store.journal(chat, 200)
    ]
    return web.json_response({"feed": lab.feed.get(chat, []), "summary": summary, "journal": journal})


async def act(request: web.Request) -> web.Response:
    data = await request.json()
    chat, action = data["chat"], data["action"]
    if not lab.ready:
        return web.json_response({"error": "нет ключа модели"}, status=503)
    if action == "text":
        lab.incoming(chat, Incoming(id=lab.next_id(), text=data["text"]), data["text"])
    elif action == "wait":
        lab.wait(chat, int(data["minutes"]))
    elif action == "image":
        kind, amount = data["kind"], data.get("amount")
        lab.brain.image = ImageCheck(kind=kind, amount=amount, recipient_ok=True, date_ok=True)
        shown = {"receipt": f"чек на {amount:g}" if amount else "чек", "other": "фото (не чек)", "unreadable": "чек, который не удалось проверить"}[kind]
        lab.incoming(chat, Incoming(id=lab.next_id(), kind="image"), shown, "image")
    elif action == "voice":
        lab.incoming(chat, Incoming(id=lab.next_id(), kind="voice"), "голосовое (не расшифровалось)", "voice")
    elif action == "manager":
        lab.engine.operator_took_over(chat, lab.now(chat))
        lab.add(chat, "system", "wait", "менеджер взял чат")
    elif action == "bot":
        lab.engine.release_to_bot(chat, lab.now(chat))
        lab.add(chat, "system", "wait", "чат возвращён боту")
    return web.json_response({"ok": True})


def main() -> None:
    app = web.Application()
    app.add_routes([
        web.get("/", page), web.get("/api/scenarios", scenarios),
        web.get("/api/state", state), web.post("/api/act", act),
        web.static("/assets", ROOT / "assets"),
    ])
    web.run_app(app, host="127.0.0.1", port=PORT, print=lambda *_: print(f"http://localhost:{PORT}"))


if __name__ == "__main__":
    main()
