"""Движок диалога: роли, переходы, счётчик вопросов, согласие, напоминания.

Здесь решается всё, что является действием: что отправить, когда выдать QR,
когда зарегистрировать заказ и кому его передать. Модель (brain.py) только
понимает реплику и пишет текст ответа на вопросы.

Роли (поле agent в журнале):
  contact   — презентация, город, блок доставки и материалов;
  consult   — ответы на вопросы по базе знаний;
  offer     — предложение оформления, скидки, реакция на отказ;
  wait      — напоминания при молчании;
  checkout  — сбор данных и оплата после согласия;
  complete  — регистрация заказа и передача менеджеру.
"""

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol
from uuid import uuid4

from . import rules as R
from .brain import Brain, Understanding
from .memory import DialogMemory, Offer, Question, Store

ORDER_OFFERS = ("order", "discount")
INFO_OFFERS = ("composition", "instruction")
MATERIALS = {
    "composition": (R.COMPOSITION, ("composition",)),
    "instruction": (R.USAGE_INSTRUCTIONS, ("usage", "usage_time")),
}
MANAGER_PHONE_TAIL = "999000845"
LEAK = re.compile(
    r"баз[аеуы]\s+знани|в\s+базе\s+нет|<NEXT_MESSAGE>|\bJSON\b"
    r"|уточн(?:ю|им)(?![а-яё])|узнаю(?![а-яё])|сообщу\s+позже|перезвон",
    re.IGNORECASE,
)
RETURNING_GREETING = "Здравствуйте! 😊 Конечно, я на связи. Что подсказать?"


def _bare(text: str) -> str:
    word = text.strip().rstrip(".!").lower()
    return word if word in ("да", "даа", "нет") else ""


def shape_bubbles(messages: list[str], questions: int) -> list[str]:
    """Формат ответа задаёт код, а не модель. Один вопрос «да/нет» — два
    пузыря: «Даа»/«Нет» и пояснение. Несколько вопросов — один цельный ответ."""
    messages = [text.strip() for text in messages if text.strip()]
    if questions <= 1:
        if len(messages) >= 2 and _bare(messages[0]):
            return ["Даа" if _bare(messages[0]) != "нет" else "Нет", " ".join(messages[1:])]
        return messages
    parts: list[str] = []
    prefix = ""
    for text in messages:
        word = _bare(text)
        if word:
            prefix = "Нет, " if word == "нет" else "Да, "
        else:
            parts.append(prefix + text[0].lower() + text[1:] if prefix else text)
            prefix = ""
    return ["\n\n".join(parts)] if parts else messages


@dataclass
class Incoming:
    id: str
    kind: str = "text"      # text | image | voice
    text: str = ""
    image: object = None
    quoted: str = ""        # сообщение, на которое клиент ответил цитатой


@dataclass
class Outgoing:
    id: str
    chat_id: str
    kind: str               # text | photo | voice | qr
    body: str
    agent: str
    why: str
    epoch: int
    after: list[str] = field(default_factory=list)   # уходит, только если эти дошли
    effects: list[tuple] = field(default_factory=list)
    reminder: bool = False
    fixed: bool = True      # готовый текст (переводится на язык клиента); False — ответ модели


class ManagerSink(Protocol):
    """Куда уходит карточка заказа или обращения. True — доставлено."""

    def send_card(self, card: dict) -> bool: ...


class Turn:
    def __init__(self, memory: DialogMemory) -> None:
        self.memory = memory
        self.out: list[Outgoing] = []
        self.handed_off = False

    def say(self, kind: str, body: str, agent: str, why: str, effects=(), after=None, reminder=False, fixed=True) -> str:
        # Случайный хвост: номер не повторится ни после сброса памяти, ни между тиками.
        msg_id = f"{self.memory.chat_id}:{self.memory.epoch}:{len(self.out)}{'r' if reminder else ''}:{uuid4().hex[:8]}"
        self.out.append(Outgoing(
            msg_id, self.memory.chat_id, kind, body, agent, why, self.memory.epoch,
            list(after or []), list(effects), reminder, fixed,
        ))
        return msg_id


class Engine:
    def __init__(self, store: Store, brain: Brain, sink: ManagerSink) -> None:
        self.store = store
        self.brain = brain
        self.sink = sink

    # ── Входящая реплика ───────────────────────────────────────────────────

    def handle(self, chat_id: str, incoming: list[Incoming], now: float) -> list[Outgoing]:
        """Несколько сообщений подряд обрабатываются как одна реплика."""
        m = self.store.load(chat_id)
        fresh = [item for item in incoming if item.id not in m.seen_ids]
        if not fresh:
            self.store.log(chat_id, "system", "дубль", "сообщение уже обработано — счётчики не меняются", now)
            return []
        m.seen_ids = (m.seen_ids + [item.id for item in fresh])[-300:]
        for item in fresh:
            self.store.add_message(chat_id, "client", item.kind, item.text or f"[{item.kind}]", now)

        # Любой ответ клиента отменяет запланированное и начинает новый эпизод
        # ожидания. Память о скидках и предложениях при этом не обнуляется.
        m.epoch += 1
        w = m.wait
        w.last_client_at, w.neutral_sent_at, w.stopped = now, 0, False
        w.chain, w.callback_at, w.checkout_reminder_sent = "silence", 0, False
        w.retry_after = 0

        turn = Turn(m)
        if m.control.mode == "manager":
            self.store.log(chat_id, "system", "молчим", "чат ведёт менеджер", now)
        elif any(item.kind == "voice" and not item.text.strip() for item in fresh):
            self._handoff(m, turn, "голосовое не расшифровано — содержание неизвестно", now)
        else:
            for item in fresh:
                if item.kind == "image":
                    self._on_image(m, turn, item, now)
            text = "\n".join(item.text.strip() for item in fresh if item.text.strip())
            if text and not turn.handed_off:
                quoted = next((item.quoted for item in fresh if item.quoted), "")
                self._on_text(m, turn, text, quoted, now)
            self._finish_order_if_ready(m, turn, now)
        self._localize(m, turn.out, now)
        self._restage(m)
        self.store.save(m)
        return turn.out

    def _localize(self, m: DialogMemory, outs: list[Outgoing], now: float) -> None:
        """Готовые тексты уходят на языке клиента. Перевод делается один раз и
        запоминается; если он не удался или в нём изменились цифры — остаётся русский."""
        lang = m.language
        todo = [out for out in outs if out.kind == "text" and out.fixed] if lang in R.LANG_NAMES else []
        missing: list[str] = []
        for out in todo:
            ready = R.approved_translation(out.body, lang) or self.store.translation(lang, out.body)
            if not ready and out.body not in missing:
                missing.append(out.body)
        translate = getattr(self.brain, "translate", None)
        if missing and translate:
            try:
                results = translate(missing, R.LANG_NAMES[lang])
            except Exception as error:
                self.store.log(m.chat_id, "system", "ОШИБКА", f"перевод не удался, текст уйдёт по-русски: {error}", now)
                results = []
            if len(results) == len(missing):
                for src, dst in zip(missing, results):
                    # Порядок слов в другом языке может переставить числа — сравниваем набор.
                    if dst.strip() and sorted(re.findall(r"\d+", src)) == sorted(re.findall(r"\d+", dst)):
                        self.store.save_translation(lang, src, dst.strip())
        for out in todo:
            out.body = R.approved_translation(out.body, lang) or self.store.translation(lang, out.body) or out.body

    def _on_text(self, m: DialogMemory, turn: Turn, text: str, quoted: str, now: float) -> None:
        lo = m.sales.last_offer
        try:
            u = self.brain.understand({
                "now": R.bishkek_iso(now),
                "facts": m.facts(),
                "last_offer": {"kind": lo.kind, "text": lo.text} if lo else None,
                "quoted": quoted,
                "history": [f"{sender}: {body}" for sender, body in self.store.recent(m.chat_id)],
                "message": text,
            })
        except Exception as error:  # сбой модели не должен терять обращение
            self.store.log(m.chat_id, "system", "ОШИБКА", f"диспетчер не ответил: {error}", now)
            return
        m.language = u.language or m.language
        self.store.log(
            m.chat_id, "dispatcher", "понял",
            f"intent={u.intent} offer_reply={u.offer_reply} zone={u.geo.zone} "
            f"вопросов={len(u.questions)} wants={u.wants or '-'}", now,
        )

        # Специальные ветки: продажа и счётчик здесь не работают.
        if u.intent == "adverse_reaction":
            ok = self._handoff(m, turn, f"жалоба на реакцию: {text}", now)
            body = R.ADVERSE_REACTION if ok else R.ADVERSE_REACTION.split("\n")[0]
            turn.say("text", body, "complete", "нежелательная реакция — продажа прекращена")
            return
        if u.intent in ("complaint", "cooperation"):
            ok = self._handoff(m, turn, f"{u.intent}: {text}", now)
            if u.intent == "complaint":
                turn.say("text", R.COMPLAINT_PROMPT, "complete", "жалоба — передано менеджеру")
            elif ok:
                turn.say("text", R.COOPERATION_REPLY, "complete", "сотрудничество — карточка у менеджера")
            return
        if u.intent == "wants_human" or (u.intent == "order_status" and m.order.status in ("registered", "handed_off")):
            self._handoff(m, turn, f"{u.intent}: {text}", now)
            return
        if u.intent == "stop_contact":
            m.control.stop_sales = True
            self.store.log(m.chat_id, "offer", "стоп", "клиент просит не писать — продажный контакт остановлен", now)
            return
        if u.offer_reply == "decline_final" and lo and lo.kind in ORDER_OFFERS and m.order.status != "collecting":
            # Окончательный отказ от покупки: вежливо отвечаем и больше не напоминаем.
            m.control.stop_sales, m.sales.last_offer_reply = True, "decline_final"
            turn.say("text", R.NOT_READY_ACK, "offer", "окончательный отказ — напоминания остановлены")
            return

        if u.callback_at:
            ts = R.parse_bishkek(u.callback_at)
            if ts and ts > now:
                m.wait.callback_at = ts
                self.store.log(m.chat_id, "wait", "отложено", f"клиент просил написать {u.callback_at}", now)

        # «Беру, только оплата при получении» — не условие, а выбор доступного способа оплаты.
        payment_only = (
            u.offer_reply == "conditional" and not u.questions
            and u.order_data.payment in R.payment_options(u.geo.zone if u.geo.mentioned else m.geo.zone)
        )
        if payment_only:
            u.offer_reply = "accept"
        info_accept = bool(lo and lo.kind in INFO_OFFERS and u.offer_reply == "accept")
        accept_order = bool(lo and lo.kind in ORDER_OFFERS and u.offer_reply == "accept")
        wants_now = u.intent == "order_now" or (u.buy_now and u.intent not in ("cancel_order", "not_ready", "order_later"))
        explicit = wants_now and u.offer_reply != "conditional" and not info_accept
        zone_before = m.geo.zone
        geo_event = self._merge_geo(m, u, now)
        self._merge_order_data(m, u)
        # Намерение купить, сохранённое до выяснения города, — тоже согласие.
        pending = m.sales.order_intent_pending and bool(m.geo.zone) and not zone_before
        consent = (accept_order or explicit or pending) and u.intent != "cancel_order"
        collecting = m.order.status == "collecting"

        first = not m.presented.price
        price_ids: list[int] = []
        if first:
            # Вопрос о цене и общий интерес («расскажите подробнее», фраза из
            # рекламы) закрыты самой презентацией — моделью их не пересказываем.
            for asked in [q for q in u.questions if q.topic in ("price", "general")]:
                u.questions.remove(asked)
                m.questions.append(Question(id=len(m.questions) + 1, text=asked.text, topic=asked.topic))
                price_ids.append(m.questions[-1].id)
            turn.say("text", R.PRESENTATION, "contact", "первое обращение — презентация с ценой",
                     [("presented", "price"), ("answered", price_ids)])
        self._geo_block(m, turn, u, geo_event, first, with_media=not (consent or collecting), now=now)
        if turn.handed_off:
            return

        answer_ids, answered_now = self._answer(m, turn, u, text, info_accept, collecting, now, just_presented=first)
        if turn.handed_off:
            return
        answered_now += len(price_ids)
        # Вопрос о городе — последним: клиент отвечает на то, что видит внизу.
        for out in [o for o in turn.out if o.body == R.CITY_QUESTION]:
            turn.out.remove(out)
            turn.out.append(out)

        # Передумал во время оформления: сбор данных прекращается, согласованная
        # цена снимается. Дальше реплика разбирается как обычная продажная.
        backs_out = u.intent in ("cancel_order", "not_ready", "order_later", "price_objection") or \
            u.offer_reply in ("decline_temporary", "decline_final")
        if collecting and backs_out:
            m.order.status, m.sales.consent_revoked, m.price.agreed = "cancelled", True, None
            self.store.log(m.chat_id, "checkout", "отмена", f"клиент передумал ({u.intent}) — оформление остановлено", now)
            collecting = False
            if u.intent != "price_objection":
                turn.say("text", R.NOT_READY_ACK, "checkout", "оформление отменено клиентом")
                return

        if collecting:
            self._checkout_turn(m, turn, u, zone_before, now)
        elif consent:
            if accept_order and lo and lo.price:
                m.price.agreed = lo.price
            self._start_checkout(m, turn, u, text, first, now)
        elif u.intent == "cancel_order":
            m.sales.order_intent_pending = False
            turn.say("text", R.NOT_READY_ACK, "offer", "клиент отказался оформлять")
        elif u.intent == "price_objection" and m.order.status in ("none", "cancelled"):
            self._discount_step(m, turn, text, now)
        elif u.offer_reply == "decline_temporary" or u.intent in ("not_ready", "order_later"):
            if lo and lo.kind in ORDER_OFFERS and u.offer_reply == "decline_temporary":
                m.sales.temporary_declines += 1
                m.sales.last_offer_reply = "decline_temporary"
                if m.sales.temporary_declines >= 2:
                    # Отдельная ветка второго отказа; обычная цепочка молчания не запускается.
                    m.wait.chain = "second_decline"
            turn.say("text", R.NOT_READY_ACK, "offer", f"временный отказ №{m.sales.temporary_declines} — без давления")
        elif answered_now and self._may_offer(m) and m.sales.answered_in_cycle + answered_now >= R.QUESTIONS_PER_OFFER:
            body = R.OFFER_FIRST if m.sales.offers_made == 0 else R.OFFER_REPEAT
            turn.say(
                "text", body, "offer",
                f"отвечено вопросов в цикле: {m.sales.answered_in_cycle}+{answered_now} — предложение №{m.sales.offers_made + 1}",
                [("offer", "order", body, m.price.offered, True)], after=answer_ids,
            )
        elif m.wait.callback_at and not turn.out:
            turn.say("text", R.NOT_READY_ACK, "wait", "клиент попросил написать позже — ждём названного времени")
        elif (u.order_data.address or u.order_data.phone) and self._may_offer(m) and not turn.handed_off:
            # Сам прислал адрес или телефон, а оформление не начато (в том числе
            # начатое прежним ботом): данные сохранены, спрашиваем согласие.
            turn.say("text", R.OFFER_FIRST, "offer", "клиент прислал данные для заказа без оформления — предлагаем оформить",
                     [("offer", "order", R.OFFER_FIRST, m.price.offered, False)], after=answer_ids[-1:])
        elif u.intent == "greeting" and not turn.out:
            turn.say("text", RETURNING_GREETING, "consult", "приветствие в уже идущем диалоге — без презентации")

    # ── География и блок материалов ────────────────────────────────────────

    def _merge_geo(self, m: DialogMemory, u: Understanding, now: float) -> str:
        g = u.geo
        if not g.mentioned:
            return ""
        if g.zone not in R.ZONES or g.zone == "unknown" or g.clarify:
            return "clarify"
        if g.zone == "unsupported":
            return "unsupported"
        if (g.zone, g.place) == (m.geo.zone, m.geo.place):
            return "same"
        if m.geo.zone:
            m.geo.corrections.append(f"{m.geo.place} → {g.place}")
        old_country = R.country_of(m.geo.zone)
        m.geo.zone, m.geo.place, m.geo.place_to, m.geo.clarify_asked = g.zone, g.place, g.place_to or g.place, 0
        country = R.country_of(g.zone)
        if country != (old_country or "KG"):
            # Другая страна — своя цена и валюта; лестница скидок КР не переносится.
            m.price.listed, m.price.currency = R.LIST_PRICE[country]
            m.price.offered, m.price.agreed, m.price.discount_level = m.price.listed, None, 0
        self.store.log(m.chat_id, "contact", "город", f"доставка в {g.place} (зона {g.zone})", now)
        return "new"

    def _geo_block(self, m, turn: Turn, u: Understanding, geo_event: str, first: bool, with_media: bool, now: float) -> None:
        if geo_event == "unsupported":
            turn.say("text", R.UNSUPPORTED_COUNTRY, "contact", "страна вне географии доставки")
        elif geo_event == "clarify":
            if m.geo.clarify_asked >= 2:
                self._handoff(m, turn, f"не удалось определить место доставки: {u.geo.place}", now)
                return
            m.geo.clarify_asked += 1
            body = R.CLARIFY_REGION if u.geo.clarify == "region" else R.CLARIFY_COUNTRY
            turn.say("text", body, "contact", "место доставки определить надёжно нельзя — уточняем, не угадываем")
        elif m.geo.zone:
            key = f"{m.geo.zone}:{m.geo.place}"
            body = R.delivery_text(m.geo.zone, m.geo.place_to)
            if body and m.presented.delivery_for != key:
                turn.say("text", body, "contact", f"доставка для зоны {m.geo.zone}", [("delivery_for", key)])
                line = R.foreign_price_line(m.geo.zone)
                if line:
                    turn.say("text", line, "contact", "цена и валюта страны доставки")
            # Фото и голосовое учитываются по отдельности и повторно не уходят.
            if not with_media:
                m.presented.media_skipped = True
            with_media = with_media and not m.presented.media_skipped
            if with_media and not m.presented.photo:
                turn.say("photo", R.PHOTO_BEFORE_AFTER, "contact", "фото до/после, без подписи", [("presented", "photo")])
            if with_media and R.VOICE_FIRST_USE not in m.presented.voices:
                turn.say("voice", R.VOICE_FIRST_USE, "contact", "голосовое про эффект", [("voice", R.VOICE_FIRST_USE)])
        elif first:
            turn.say("text", R.CITY_QUESTION, "contact", "город неизвестен", [("presented", "city_question")])

    # ── Ответы на вопросы ──────────────────────────────────────────────────

    def _answer(self, m, turn: Turn, u: Understanding, text: str, info_accept: bool, collecting: bool, now: float,
                just_presented: bool = False):
        """Возвращает (id сообщений-ответов, сколько вопросов пойдёт в счётчик)."""
        lo = m.sales.last_offer
        questions = []
        for asked in u.questions:
            q = Question(id=len(m.questions) + 1, text=asked.text, topic=asked.topic)
            m.questions.append(q)
            questions.append(q)
        answer_ids: list[str] = []
        counted: list[int] = []

        def closes(topics) -> list[int]:
            ids = [q.id for q in questions if q.topic in topics and q.status == "open"]
            for q in questions:
                if q.id in ids:
                    q.status = "answering"
            return ids

        want = u.wants or (lo.kind if info_accept and lo else "")
        if want in MATERIALS:
            body, topics = MATERIALS[want]
            ids = closes(topics)
            answer_ids.append(turn.say(
                "text", body, "consult", f"клиент просил: {want}",
                [("material", want), ("answered", ids)],
            ))
            counted += ids
        trust = [q for q in questions if q.topic == "trust" and q.status == "open"]
        if trust and R.VOICE_NOT_SCAM not in m.presented.voices:
            ids = closes(("trust",))
            answer_ids.append(turn.say(
                "voice", R.VOICE_NOT_SCAM, "consult", "сомнение в магазине — готовое голосовое",
                [("voice", R.VOICE_NOT_SCAM), ("answered", ids)],
            ))
            counted += ids

        # Документы и сертификаты: подтверждённого ответа нет — отвечает человек.
        documents = [q for q in questions if q.topic == "documents"]
        for q in documents:
            q.status = "unknown"
        rest = [q for q in questions if q.status == "open"]
        if rest:
            role = "checkout" if collecting else "objection" if lo and lo.kind in ORDER_OFFERS else "consult"
            will_offer = m.sales.answered_in_cycle + len(counted) + len(rest) >= R.QUESTIONS_PER_OFFER
            try:
                written = self.brain.write(role, {
                    "facts": m.facts(),
                    "history": [f"{sender}: {body}" for sender, body in self.store.recent(m.chat_id, 8)],
                    "message": text,
                    "questions": [{"n": n, "text": q.text, "topic": q.topic} for n, q in enumerate(rest, 1)],
                    "may_offer_material": not will_offer and not collecting,
                    "just_presented": just_presented,
                })
            except Exception as error:
                self.store.log(m.chat_id, "system", "ОШИБКА", f"роль {role} не ответила: {error}", now)
                return answer_ids, 0 if collecting else len(counted)
            # Служебные слова в тексте — признак, что ответа нет: клиенту такое не уходит.
            if any(LEAK.search(body) for body in written.messages):
                self.store.log(m.chat_id, role, "отброшено", "в ответе служебные слова — вопрос уходит менеджеру", now)
                written.messages, written.answered, written.unknown = [], [], list(range(1, len(rest) + 1))
            written.messages = shape_bubbles(written.messages, len(rest))
            ids = [rest[n - 1].id for n in written.answered if 0 < n <= len(rest)] if written.messages else []
            unknown = [rest[n - 1] for n in written.unknown if 0 < n <= len(rest)]
            for index, body in enumerate(written.messages):
                last = index == len(written.messages) - 1
                effects = [("answered", ids)] if last else []
                if last and written.offer in INFO_OFFERS and not will_offer and not collecting:
                    effects.append(("offer", written.offer, body, None, False))
                answer_ids.append(turn.say(
                    "text", body, role, f"ответ на вопросы: {[q.text for q in rest]}", effects, after=answer_ids[-1:],
                    fixed=False,
                ))
            counted += ids
            for q in unknown:
                q.status = "unknown"
        unknown_all = [q for q in questions if q.status == "unknown"]
        if unknown_all:
            self._handoff(m, turn, "нет подтверждённого ответа: " + "; ".join(q.text for q in unknown_all), now)
        for q in questions:
            if q.status == "answering":
                q.status = "open"   # закроется, когда ответ реально уйдёт
        # Вопросы во время оформления в продажный счётчик не идут.
        return answer_ids, 0 if collecting else len(counted)

    def _may_offer(self, m: DialogMemory) -> bool:
        return m.order.status in ("none", "cancelled") and not m.control.stop_sales and m.control.mode == "bot"

    def _discount_step(self, m, turn: Turn, text: str, now: float) -> None:
        if R.country_of(m.geo.zone) not in ("", "KG"):
            self.store.log(m.chat_id, "offer", "скидки нет", "лестница скидок действует только в Кыргызстане", now)
            return
        level = m.price.discount_level
        m.price.discount_reason = text[:200]
        if level == 0:
            turn.say("text", R.DISCOUNT_1, "offer", "ценовое возражение — первая ступень",
                     [("discount", 1), ("offer", "discount", R.DISCOUNT_1, R.DISCOUNT_LADDER[0], False)])
        elif level == 1:
            turn.say("text", R.DISCOUNT_2, "offer", "повторное ценовое возражение — вторая ступень",
                     [("discount", 2), ("offer", "discount", R.DISCOUNT_2, R.DISCOUNT_LADDER[1], False)])
        else:
            turn.say("text", R.DISCOUNT_FLOOR, "offer", "ниже 1190 не предлагаем",
                     [("offer", "discount", R.DISCOUNT_FLOOR, R.DISCOUNT_LADDER[1], False)])

    # ── Оформление ─────────────────────────────────────────────────────────

    def _merge_order_data(self, m: DialogMemory, u: Understanding) -> None:
        d, o = u.order_data, m.order
        if d.quantity and d.quantity > 0:
            o.quantity, o.quantity_assumed = d.quantity, False
        if d.address.strip():
            o.address = d.address.strip()
        digits = re.sub(r"\D", "", d.phone)
        # Номер менеджера из наших же сообщений телефоном клиента не становится.
        if 9 <= len(digits) <= 13 and not digits.endswith(MANAGER_PHONE_TAIL):
            o.phone = d.phone.strip()
        if d.recipient_name.strip():
            o.recipient_name = d.recipient_name.strip()

    def _start_checkout(self, m, turn: Turn, u: Understanding, text: str, first: bool, now: float) -> None:
        if m.order.status in ("registered", "handed_off"):
            self._handoff(m, turn, f"клиент с оформленным заказом хочет заказать снова: {text}", now)
            return
        if not m.geo.zone:
            m.sales.order_intent_pending = True
            if not first and not any(o.body in (R.CITY_QUESTION, R.CLARIFY_COUNTRY, R.CLARIFY_REGION) for o in turn.out):
                turn.say("text", R.CITY_QUESTION, "contact", "готов купить, но без города нет условий оплаты и доставки")
            return
        options = R.payment_options(m.geo.zone)
        if not options:
            self._handoff(m, turn, f"согласие на заказ, но условия оплаты для «{m.geo.place}» не определены", now)
            return
        o, s = m.order, m.sales
        s.consent_text, s.consent_at, s.consent_revoked, s.order_intent_pending = text[:300], now, False, False
        o.status, o.receipt, o.receipt_note, o.receipt_ids = "collecting", "none", "", []
        m.price.agreed = m.price.agreed or m.price.offered
        if o.quantity is None:
            o.quantity, o.quantity_assumed = 1, True
        o.payment = u.order_data.payment if u.order_data.payment in options else (o.payment if o.payment in options else "full")
        self.store.log(m.chat_id, "checkout", "согласие", f"«{text[:80]}» — цена {m.price.agreed}, оплата {o.payment}", now)
        self._checkout_prompt(m, turn, "клиент согласился — оформление")

    def _missing(self, m: DialogMemory) -> list[str]:
        o, missing = m.order, []
        if o.payment != "cod" and o.receipt == "none":
            missing.append("чек об оплате")
        if not o.address:
            missing.append("адрес доставки")
        if not o.phone:
            missing.append("номер телефона")
        return missing

    def _checklist(self, missing: list[str]) -> str:
        return R.CHECKLIST_HEAD + "\n\n" + "\n".join(f"• {item};" for item in missing)[:-1] + "."

    def _checkout_prompt(self, m, turn: Turn, why: str) -> None:
        o, missing = m.order, self._missing(m)
        if not missing:
            return
        if o.payment == "cod":
            turn.say("text", f"{R.COD_ACK}\n\n{self._checklist(missing)}", "checkout", why)
            return
        qr_key = f"qr:{o.payment}"
        qr_id = []
        if qr_key not in m.presented.materials:
            qr_id = [turn.say("qr", R.QR_IMAGE, "checkout", "QR после согласия", [("material", qr_key)])]
        body = self._checklist(missing)
        if o.payment == "prepay":
            body = f"{R.PREPAY_NOTE}\n\n{body}"
        turn.say("text", body, "checkout", why, after=qr_id)

    def _checkout_turn(self, m, turn: Turn, u: Understanding, zone_before: str, now: float) -> None:
        o = m.order
        options = R.payment_options(m.geo.zone)
        if not options:
            self._handoff(m, turn, f"во время оформления сменилось направление: «{m.geo.place}», условий оплаты нет", now)
            return
        changed = m.geo.zone != zone_before
        wanted = u.order_data.payment
        if wanted and wanted in options and wanted != o.payment:
            o.payment, changed = wanted, True
        elif o.payment not in options:
            o.payment, changed = "full", True
        if changed:
            m.price.agreed = m.price.agreed or m.price.offered
            self._checkout_prompt(m, turn, "условия изменились — пересчитано")
        elif self._missing(m):
            turn.say("text", self._checklist(self._missing(m)), "checkout", "оформление продолжается с сохранённого места",
                     after=[x.id for x in turn.out[-1:]])

    def _expected_amount(self, m: DialogMemory) -> int:
        if m.order.payment == "prepay":
            return R.PREPAY_AMOUNT
        return (m.price.agreed or m.price.offered) * (m.order.quantity or 1)

    def _on_image(self, m, turn: Turn, item: Incoming, now: float) -> None:
        o = m.order
        try:
            check = self.brain.check_image(item.image)
        except Exception as error:
            self.store.log(m.chat_id, "system", "ОШИБКА", f"проверка картинки недоступна: {error}", now)
            check = None
        kind = check.kind if check else "unreadable"
        expecting = o.status == "collecting" and o.payment in ("full", "prepay")
        if kind == "other":
            if expecting:
                turn.say("text", R.NOT_A_RECEIPT, "checkout", "картинка не чек — заказ не создаём")
            else:
                self.store.log(m.chat_id, "system", "картинка", "не чек и чек не ожидается — заказ не создаём", now)
            return
        if not expecting:
            if kind == "receipt" and o.status in ("none", "cancelled"):
                self._handoff(m, turn, "клиент прислал чек без оформления — нужна ручная проверка", now)
            else:
                self.store.log(m.chat_id, "system", "картинка", f"чек не ожидается (заказ: {o.status}) — нового заказа нет", now)
            return
        o.receipt_ids.append(item.id)
        expected = self._expected_amount(m)
        problems = []
        if kind == "unreadable":
            problems.append("автопроверка недоступна или чек не прочитан")
        else:
            if check.amount is None:
                problems.append("сумма на чеке не видна")
            elif abs(check.amount - expected) > 0.5:
                problems.append(f"сумма {check.amount:g} вместо {expected} {m.price.currency}")
            if check.recipient_ok is False:
                problems.append("получатель не совпадает")
            if check.date_ok is False:
                problems.append("дата чека не сегодняшняя")
        o.receipt = "manual_review" if problems else "check_ok"
        o.receipt_note = "; ".join(problems)
        self.store.log(m.chat_id, "checkout", "чек", f"{o.receipt}: {o.receipt_note or 'совпал с ожидаемым'}", now)

    # ── Завершение и передача ──────────────────────────────────────────────

    def _order_card(self, m: DialogMemory) -> dict:
        o = m.order
        return {
            "type": "order", "chat_id": m.chat_id, "place": m.geo.place, "zone": m.geo.zone,
            "quantity": o.quantity, "quantity_assumed": o.quantity_assumed,
            "price": m.price.agreed, "currency": m.price.currency, "total": (m.price.agreed or 0) * (o.quantity or 1),
            "payment": o.payment, "prepay": R.PREPAY_AMOUNT if o.payment == "prepay" else None,
            "receipt": o.receipt, "receipt_note": o.receipt_note,
            "address": o.address, "phone": o.phone, "recipient_name": o.recipient_name,
            "consent": m.sales.consent_text, "discount_reason": m.price.discount_reason if m.price.discount_level else "",
            "geo_corrections": m.geo.corrections,
            "receipt_message_id": o.receipt_ids[-1] if o.receipt_ids else "",
        }

    def _send_card(self, m: DialogMemory, card: dict, now: float) -> bool:
        try:
            ok = bool(self.sink.send_card(card))
        except Exception as error:
            self.store.log(m.chat_id, "complete", "ОШИБКА", f"передача менеджеру упала: {error}", now)
            ok = False
        self.store.log(m.chat_id, "complete", "передача" if ok else "ПЕРЕДАЧА НЕ УДАЛАСЬ", str(card.get("type")), now)
        return ok

    def _finish_order_if_ready(self, m, turn: Turn, now: float) -> None:
        o = m.order
        if o.status != "collecting" or self._missing(m) or turn.handed_off:
            return
        card = self._order_card(m)
        # Ключ — момент согласия: повторный чек или вебхук второй заказ не создаст.
        o.order_id, created = self.store.register_order(m.chat_id, f"{m.chat_id}:{m.sales.consent_at}", card)
        o.status = "registered"
        self.store.log(m.chat_id, "complete", "заказ", f"№{o.order_id} {'создан' if created else 'уже был'}", now)
        ok = self._send_card(m, {**card, "order_id": o.order_id}, now)
        o.handoff, o.handoff_attempts = ("ok" if ok else "failed"), 1
        if ok:
            o.status = "handed_off"
        if o.payment == "cod":
            body = R.ORDER_DONE_COD
        elif o.receipt == "manual_review":
            body = R.ORDER_MANUAL_REVIEW if ok else "Спасибо! Чек получила, он на проверке 😊"
        else:
            body = R.ORDER_DONE_PAID if ok else R.ORDER_DONE_PAID.split(" — ")[0] + "."
        turn.say("text", body, "complete", f"заказ №{o.order_id} зарегистрирован, передача: {o.handoff}")

    def _handoff(self, m, turn: Turn, reason: str, now: float) -> bool:
        ok = self._send_card(m, self._handoff_card(m, reason), now)
        m.control.mode, m.control.handoff_reason, m.control.handoff_pending = "manager", reason, not ok
        turn.handed_off = True
        self.store.log(m.chat_id, "complete", "менеджеру", reason, now)
        return ok

    def _handoff_card(self, m: DialogMemory, reason: str) -> dict:
        return {"type": "handoff", "chat_id": m.chat_id, "reason": reason, "place": m.geo.place, "facts": m.facts()}

    def _restage(self, m: DialogMemory) -> None:
        lo = m.sales.last_offer
        if m.order.status == "collecting":
            m.stage = "checkout"
        elif m.order.status in ("registered", "handed_off"):
            m.stage = "complete"
        elif not m.geo.zone:
            m.stage = "contact"
        elif lo and lo.kind in ORDER_OFFERS and lo.at > m.wait.last_client_at:
            m.stage = "offer"
        else:
            m.stage = "consult"

    # ── Отправка ───────────────────────────────────────────────────────────

    def can_send(self, out: Outgoing, now: float) -> bool:
        """Проверка перед самой отправкой: устаревшее не уходит."""
        m = self.store.load(out.chat_id)
        if out.epoch != m.epoch or m.control.mode == "manager" and out.agent != "complete":
            return False
        if out.reminder and (R.is_quiet_hours(now) or m.control.stop_sales or m.wait.last_client_at > m.wait.last_bot_sent_at):
            return False
        return True

    def delivered(self, out: Outgoing, ok: bool, now: float, uncertain: bool = False) -> None:
        """Память меняется только по факту отправки, а не по черновику.
        uncertain — сервис отправки ответил ошибкой, но сообщение могло дойти:
        считаем отправленным, чтобы клиент не получил его второй раз."""
        m = self.store.load(out.chat_id)
        if not ok:
            self.store.log(out.chat_id, out.agent, "НЕ ОТПРАВЛЕНО", f"{out.kind}: {out.body[:60]}", now)
            if out.reminder:
                # Не повторяем каждую минуту: сбой отправки не должен превращаться в поток попыток.
                m.wait.retry_after = now + R.REMINDER_RETRY_AFTER
                self.store.save(m)
            return
        if uncertain:
            self.store.log(out.chat_id, out.agent, "ВОЗМОЖНО ДОСТАВЛЕНО", f"сервис отправки ответил ошибкой: {out.body[:60]}", now)
        for effect in out.effects:
            name = effect[0]
            if name == "presented":
                setattr(m.presented, effect[1], True)
            elif name == "delivery_for":
                m.presented.delivery_for = effect[1]
            elif name == "voice":
                m.presented.voices.append(effect[1])
            elif name == "material":
                m.presented.materials.append(effect[1])
            elif name == "answered":
                for q in m.questions:
                    if q.id in effect[1] and q.status != "answered":
                        q.status = "answered"
                        if m.order.status != "collecting" and not q.counted:
                            q.counted = True
                            m.sales.answered_in_cycle += 1
            elif name == "offer":
                _, kind, text, price, counts = effect
                m.sales.last_offer = Offer(kind=kind, text=text, price=price, at=now)
                m.sales.last_offer_reply = ""
                if counts:   # предложение по счётчику открывает новый цикл вопросов
                    m.sales.offers_made += 1
                    m.sales.answered_in_cycle = 0
            elif name == "discount":
                m.price.discount_level = effect[1]
                m.price.offered = R.DISCOUNT_LADDER[effect[1] - 1]
            elif name == "neutral":
                m.wait.neutral_sent_at = now
            elif name == "stop":
                m.wait.stopped = True
            elif name == "checkout_reminder":
                m.wait.checkout_reminder_sent = True
        m.wait.last_bot_sent_at = now
        self.store.add_message(out.chat_id, "bot", out.kind, out.body, now)
        self.store.log(out.chat_id, out.agent, f"отправлено {out.kind}", out.why, now)
        self._restage(m)
        self.store.save(m)

    def dispatch(self, outs: list[Outgoing], send: Callable[[Outgoing], bool], now: float) -> list[Outgoing]:
        """Отправляет по порядку. Возвращает то, что реально ушло."""
        sent, failed = [], set()
        for out in outs:
            if any(dep in failed for dep in out.after):
                failed.add(out.id)
                continue
            if not self.can_send(out, now):
                failed.add(out.id)
                self.store.log(out.chat_id, out.agent, "отменено", f"устарело до отправки: {out.body[:60]}", now)
                continue
            ok = bool(send(out))
            self.delivered(out, ok, now)
            (sent.append(out) if ok else failed.add(out.id))
        return sent

    # ── Напоминания при молчании ───────────────────────────────────────────

    def tick(self, chat_id: str, now: float) -> list[Outgoing]:
        """Вызывается по таймеру. Возвращает не больше одного напоминания."""
        m = self.store.load(chat_id)
        before = m.model_dump_json()
        turn = Turn(m)
        self._retry_handoffs(m, now)
        w, o = m.wait, m.order
        silent = w.last_bot_sent_at and w.last_client_at <= w.last_bot_sent_at
        blocked = (
            m.control.mode == "manager" or m.control.stop_sales or w.stopped or not silent
            or o.status in ("registered", "handed_off") or R.is_quiet_hours(now)
            or (w.callback_at and now < w.callback_at) or now < w.retry_after
        )
        if not blocked and not w.callback_at and now - w.last_bot_sent_at > R.REMINDER_MAX_AGE:
            # После простоя или долгой паузы не пишем людям, молчавшим со вчера.
            w.stopped = True
            self.store.log(chat_id, "wait", "стоп", "напоминание устарело — ждём ответа клиента", now)
            blocked = True
        if not blocked:
            if o.status == "collecting":
                if not w.checkout_reminder_sent and now - w.last_bot_sent_at >= R.NEUTRAL_AFTER and self._missing(m):
                    turn.say("text", self._checklist(self._missing(m)), "wait", "оформление идёт — напоминаем о недостающем",
                             [("checkout_reminder",), ("stop",)], reminder=True)
            elif w.chain == "second_decline":
                if now - w.last_bot_sent_at >= R.DISCOUNT_AFTER:
                    self._discount_reminder(m, turn, "второй временный отказ, час молчания", now)
            elif not w.neutral_sent_at:
                due = w.callback_at or w.last_bot_sent_at + R.NEUTRAL_AFTER
                if now >= due:
                    if m.geo.zone:
                        turn.say("text", R.REMINDER_NEUTRAL, "wait", "30 минут молчания — нейтральное напоминание",
                                 [("neutral",), ("offer", "order", R.REMINDER_NEUTRAL, m.price.offered, False)], reminder=True)
                    else:
                        turn.say("text", R.REMINDER_NO_CITY, "wait", "30 минут молчания, города нет — возвращаемся к городу",
                                 [("neutral",)], reminder=True)
            elif now - w.neutral_sent_at >= R.DISCOUNT_AFTER:
                self._discount_reminder(m, turn, "час молчания после напоминания", now)
        self._localize(m, turn.out, now)
        if m.model_dump_json() != before:
            self.store.save(m)
        return turn.out

    def _discount_reminder(self, m, turn: Turn, why: str, now: float) -> None:
        if m.price.discount_level == 0 and R.country_of(m.geo.zone) == "KG":
            turn.say("text", R.DISCOUNT_1, "wait", f"{why} — первая ступень скидки, дальше ждём ответа",
                     [("discount", 1), ("offer", "discount", R.DISCOUNT_1, R.DISCOUNT_LADDER[0], False), ("stop",)], reminder=True)
        else:
            # 1290 уже предлагали (или страна не КР): молчание цену не снижает.
            m.wait.stopped = True
            self.store.log(m.chat_id, "wait", "стоп", f"{why}: скидочное сообщение неуместно — ждём ответа клиента", now)

    def _retry_handoffs(self, m: DialogMemory, now: float) -> None:
        if m.control.handoff_pending:
            m.control.handoff_pending = not self._send_card(m, self._handoff_card(m, m.control.handoff_reason), now)
        if m.order.status == "registered" and m.order.handoff == "failed":
            m.order.handoff_attempts += 1
            if self._send_card(m, {**self._order_card(m), "order_id": m.order.order_id}, now):
                m.order.handoff, m.order.status = "ok", "handed_off"

    # ── Связь с транспортом ────────────────────────────────────────────────

    def seed(self, chat_id: str, has_history: bool, order_placed: bool, now: float) -> None:
        """Клиент уже общался с прежним ботом: презентацию и материалы он
        видел, заново их не шлём. Срабатывает один раз, до первой реплики."""
        m = self.store.load(chat_id)
        if m.epoch or not (has_history or order_placed):
            return
        p = m.presented
        p.price = p.city_question = p.photo = p.media_skipped = True
        p.voices.append(R.VOICE_FIRST_USE)
        if order_placed:
            m.order.status, m.order.handoff = "handed_off", "ok"
        self.store.log(chat_id, "system", "подхват", f"чат из прежнего бота: история есть, заказ {'есть' if order_placed else 'нет'}", now)
        self._restage(m)
        self.store.save(m)

    def card_failed(self, chat_id: str, card_type: str, now: float) -> None:
        """Транспорт не смог доставить карточку — помечаем для повтора по таймеру."""
        m = self.store.load(chat_id)
        if card_type == "order" and m.order.status in ("registered", "handed_off"):
            m.order.status, m.order.handoff = "registered", "failed"
        elif card_type == "handoff":
            m.control.handoff_pending = True
        self.store.log(chat_id, "complete", "ПЕРЕДАЧА НЕ УДАЛАСЬ", f"карточка {card_type} не дошла — повторим", now)
        self.store.save(m)

    def stop_all_reminders(self, now: float) -> int:
        """Аварийная кнопка: ни одному из уже известных чатов напоминание не
        уйдёт, пока клиент сам не напишет. Нужна после простоя или отката,
        когда память движка отстала от реальной переписки."""
        count = 0
        for chat_id in self.store.chat_ids():
            m = self.store.load(chat_id)
            if not m.wait.stopped:
                m.wait.stopped = True
                self.store.save(m)
                count += 1
        self.store.log("*", "system", "стоп", f"напоминания остановлены для {count} чатов", now)
        return count

    # ── Управление менеджера ───────────────────────────────────────────────

    def operator_took_over(self, chat_id: str, now: float) -> None:
        m = self.store.load(chat_id)
        m.control.mode = "manager"
        self.store.log(chat_id, "system", "менеджер", "человек начал вести чат — бот и напоминания остановлены", now)
        self.store.save(m)

    def release_to_bot(self, chat_id: str, now: float) -> None:
        m = self.store.load(chat_id)
        m.control.mode, m.control.handoff_pending = "bot", False
        self.store.log(chat_id, "system", "бот", "чат возвращён боту через управление", now)
        self.store.save(m)
