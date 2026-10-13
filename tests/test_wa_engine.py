"""Сценарии из задания на уровне движка.

Модель здесь подменена: тест сам задаёт, как диспетчер понял реплику.
Поэтому проверяется логика действий (что уходит, когда, по какому
основанию), а не качество понимания речи моделью.
"""

from datetime import datetime

import pytest

from smilekit_bot.wa import rules as R
from smilekit_bot.wa.brain import AskedQuestion, GeoSeen, ImageCheck, OrderData, Understanding, Written
from smilekit_bot.wa.engine import Engine, Incoming, shape_bubbles
from smilekit_bot.wa.memory import Offer, Store

CHAT = "996700000001"


def at(hour: int, minute: int = 0, day: int = 12) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=R.BISHKEK).timestamp()


def geo(zone: str, place: str, place_to: str = "", clarify: str = "") -> GeoSeen:
    return GeoSeen(mentioned=True, zone=zone, place=place, place_to=place_to or place, clarify=clarify)


def qs(*topics: str) -> list[AskedQuestion]:
    return [AskedQuestion(text=f"вопрос про {topic}", topic=topic) for topic in topics]


OSH = geo("kg_other", "Ош")
BISHKEK = geo("bishkek", "Бишкек")


class FakeBrain:
    def __init__(self) -> None:
        self.next = Understanding()
        self.image = ImageCheck(kind="other")
        self.write_calls = 0
        self.unknown: list[int] = []

    def understand(self, context: dict) -> Understanding:
        return self.next

    def write(self, role: str, context: dict) -> Written:
        self.write_calls += 1
        numbers = [q["n"] for q in context["questions"]]
        return Written(
            messages=[f"ответ[{role}] на {len(numbers)}"],
            answered=[n for n in numbers if n not in self.unknown],
            unknown=self.unknown,
        )

    def check_image(self, image: object) -> ImageCheck:
        return self.image


class Sink:
    def __init__(self) -> None:
        self.cards: list[dict] = []
        self.ok = True

    def send_card(self, card: dict) -> bool:
        if self.ok:
            self.cards.append(card)
        return self.ok


class Bot:
    def __init__(self) -> None:
        self.brain, self.sink, self.store = FakeBrain(), Sink(), Store(":memory:")
        self.engine = Engine(self.store, self.brain, self.sink)
        self.n = 0
        self.fail_kinds: set[str] = set()

    def _send(self, out) -> bool:
        return out.kind not in self.fail_kinds

    def say(self, text: str, now: float, kind: str = "text", **understood) -> list:
        self.n += 1
        return self.msg(Incoming(id=f"m{self.n}", kind=kind, text=text), now, **understood)

    def msg(self, item: Incoming, now: float, **understood) -> list:
        self.brain.next = Understanding(**understood)
        return self.engine.dispatch(self.engine.handle(CHAT, [item], now), self._send, now)

    def tick(self, now: float) -> list:
        return self.engine.dispatch(self.engine.tick(CHAT, now), self._send, now)

    @property
    def mem(self):
        return self.store.load(CHAT)

    def presented(self, now: float = at(12)) -> "Bot":
        """Клиент прошёл презентацию и назвал Ош."""
        self.say("Здравствуйте", now, intent="greeting")
        self.say("Ош", now, geo=OSH)
        return self

    def offered(self, now: float = at(12)) -> "Bot":
        self.presented(now)
        self.say("три вопроса", now, intent="question", questions=qs("price", "usage_time", "return"))
        return self

    def in_checkout(self, now: float = at(12)) -> "Bot":
        self.offered(now)
        self.say("Да, давайте", now, intent="order_now", offer_reply="accept")
        return self


def bodies(outs) -> list[str]:
    return [out.body for out in outs]


def kinds(outs) -> list[str]:
    return [out.kind for out in outs]


@pytest.fixture
def bot() -> Bot:
    return Bot()


# 1
def test_first_hello_gives_presentation_then_separate_city_question(bot):
    assert bodies(bot.say("Здравствуйте", at(12), intent="greeting")) == [R.PRESENTATION, R.CITY_QUESTION]


# 2
def test_city_in_first_message_is_not_asked_again(bot):
    outs = bot.say("Я из Оша, сколько стоит?", at(12), intent="question", geo=OSH, questions=qs("price"))
    assert R.CITY_QUESTION not in bodies(outs)
    assert bodies(outs)[:2] == [R.PRESENTATION, "Да, у нас есть доставка в Ош через Ылдам Экспресс 🚚✅\nЗа 1 день доставим прямо до дома."]
    assert kinds(outs)[2:4] == ["photo", "voice"]


# 3
def test_tokmok_gets_its_own_text(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    text = bodies(bot.say("Токмок", at(12), geo=geo("tokmok", "Токмок")))[0]
    assert "в Токмок через Яндекс" in text and "в течение 30 минут" in text and "Бишкек" not in text


# 4
def test_village_outside_old_list_gets_regional_delivery(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    text = bodies(bot.say("село Арчалы", at(12), geo=geo("kg_other", "Арчалы")))[0]
    assert "в Арчалы через Ылдам Экспресс" in text


# 5
def test_unknown_place_is_clarified_not_guessed(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    outs = bot.say("Кызыл-Тоо", at(12), geo=geo("unknown", "Кызыл-Тоо", clarify="country"))
    assert bodies(outs) == [R.CLARIFY_COUNTRY]
    assert bot.mem.geo.zone == ""


# 6 — понимание «нахожусь там, отправить сюда» делает диспетчер; движок берёт место доставки
def test_delivery_place_wins_and_correction_replaces_city(bot):
    bot.presented()
    outs = bot.say("Нет, отправить нужно в Бишкек", at(12), geo=BISHKEK)
    assert "по Бишкеку" in bodies(outs)[0]
    assert kinds(outs) == ["text"], "фото и голосовое повторно не уходят"
    assert bot.mem.geo.corrections == ["Ош → Бишкек"]


# 7
def test_question_instead_of_city_is_answered(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    outs = bot.say("А сколько держится эффект?", at(12), intent="question", questions=qs("result_duration"))
    assert bodies(outs) == ["ответ[consult] на 1"]


# 8
def test_three_questions_in_one_message_then_single_offer(bot):
    bot.presented()
    outs = bot.say("...", at(12), intent="question", questions=qs("pack_contents", "usage_time", "delivery_cost"))
    assert bodies(outs) == ["ответ[consult] на 3", R.OFFER_FIRST]
    outs = bot.say("....", at(12), intent="question", questions=qs("a", "b", "c", "d", "e"))
    assert bodies(outs).count(R.OFFER_REPEAT) == 1


# 9
def test_one_question_and_non_questions_do_not_trigger_offer(bot):
    bot.presented()
    bot.say("сколько", at(12), intent="question", questions=qs("price"))
    for text in ("спасибо", "понятно", "хорошо"):
        assert bot.say(text, at(12), intent="smalltalk") == []
    assert bot.mem.sales.answered_in_cycle == 1 and bot.mem.sales.offers_made == 0


def test_duplicate_webhook_does_not_count_twice(bot):
    bot.presented()
    item = Incoming(id="dup", text="сколько?")
    bot.msg(item, at(12), intent="question", questions=qs("price"))
    assert bot.msg(item, at(12), intent="question", questions=qs("price")) == []
    assert bot.mem.sales.answered_in_cycle == 1


def test_unsent_answer_is_not_counted(bot):
    bot.presented()
    bot.fail_kinds = {"text"}
    assert bot.say("...", at(12), intent="question", questions=qs("a", "b", "c")) == []
    assert bot.mem.sales.answered_in_cycle == 0 and bot.mem.sales.last_offer is None


# 10
def test_yes_to_composition_offer_sends_composition_not_qr(bot):
    bot.presented()
    bot.say("состав безопасный?", at(12), intent="question", questions=qs("safety"))
    m = bot.mem
    m.sales.last_offer = Offer(kind="composition", text="Могу прислать состав")
    bot.store.save(m)
    # даже если диспетчер ошибся и назвал это заказом — действие заказа не начинается
    outs = bot.say("Да", at(12), intent="order_now", offer_reply="accept")
    assert bodies(outs) == [R.COMPOSITION]
    assert bot.mem.order.status == "none"


# 11
def test_yes_to_order_offer_starts_checkout(bot):
    bot.offered()
    outs = bot.say("Да, давайте", at(12), intent="smalltalk", offer_reply="accept")
    assert kinds(outs) == ["qr", "text"]
    assert "• чек об оплате;\n• адрес доставки;\n• номер телефона." in bodies(outs)[1]
    assert bot.mem.order.status == "collecting" and bot.mem.stage == "checkout"


# 12
def test_conditional_yes_answers_first(bot):
    bot.offered()
    outs = bot.say("Да, но сначала скажите стоимость доставки", at(12), intent="order_now",
                   offer_reply="conditional", questions=qs("delivery_cost"))
    assert bodies(outs) == ["ответ[objection] на 1"]
    assert bot.mem.order.status == "none"


# 13
def test_future_question_and_refusal_do_not_start_payment(bot):
    bot.offered()
    assert "qr" not in kinds(bot.say("Можно завтра заказать?", at(12), intent="order_later", questions=qs("how_to_order")))
    assert bodies(bot.say("Не надо оформлять", at(12), intent="cancel_order")) == [R.NOT_READY_ACK]
    assert bot.mem.order.status == "none"


# 14
def test_half_box_is_a_question(bot):
    bot.presented()
    outs = bot.say("Можно купить половину коробки?", at(12), intent="question", questions=qs("pack_contents"))
    assert kinds(outs) == ["text"] and bot.mem.order.status == "none"


# 15
def test_decline_then_new_cycle_of_three(bot):
    bot.offered()
    assert bodies(bot.say("пока нет", at(12), intent="not_ready", offer_reply="decline_temporary")) == [R.NOT_READY_ACK]
    assert bodies(bot.say("?", at(12), intent="question", questions=qs("a"))) == ["ответ[objection] на 1"]
    assert bodies(bot.say("?", at(12), intent="question", questions=qs("b"))) == ["ответ[objection] на 1"]
    assert bodies(bot.say("?", at(12), intent="question", questions=qs("c")))[-1] == R.OFFER_REPEAT
    assert bot.mem.sales.offers_made == 2


def test_no_to_other_offer_is_not_order_decline(bot):
    bot.presented()
    m = bot.mem
    m.sales.last_offer = Offer(kind="instruction", text="Прислать инструкцию?")
    bot.store.save(m)
    bot.say("нет", at(12), intent="smalltalk", offer_reply="decline_temporary")
    assert bot.mem.sales.temporary_declines == 0


# 16
def test_second_decline_gives_one_discount_after_hour_without_parallel_chain(bot):
    bot.offered(at(12))
    bot.say("пока нет", at(12), intent="not_ready", offer_reply="decline_temporary")
    bot.say("?", at(12), intent="question", questions=qs("a", "b", "c"))
    bot.say("пока не готов", at(12), intent="not_ready", offer_reply="decline_temporary")
    assert bot.tick(at(12, 31)) == [], "обычное 30-минутное напоминание не запускается"
    assert bodies(bot.tick(at(13, 1))) == [R.DISCOUNT_1]
    assert bot.tick(at(15)) == [] and bot.tick(at(12, day=13)) == []


# 17
def test_silence_chain_then_stop(bot):
    bot.presented(at(12))
    assert bot.tick(at(12, 29)) == []
    assert bodies(bot.tick(at(12, 30))) == [R.REMINDER_NEUTRAL]
    assert bot.tick(at(13, 29)) == []
    assert bodies(bot.tick(at(13, 30))) == [R.DISCOUNT_1]
    assert bot.tick(at(14, 30)) == [] and bot.tick(at(13, 30, day=13)) == []
    assert bot.mem.price.offered == 1290


def test_silence_without_city_returns_to_city_and_gives_no_discount(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    assert bodies(bot.tick(at(12, 30))) == [R.REMINDER_NO_CITY]
    assert bot.tick(at(13, 30)) == [] and bot.mem.price.discount_level == 0


# 18
def test_reply_just_before_reminder_cancels_it(bot):
    bot.presented(at(12))
    pending = bot.engine.tick(CHAT, at(12, 30))
    assert len(pending) == 1
    bot.engine.handle(CHAT, [Incoming(id="late", text="а состав?")], at(12, 30))
    assert bot.engine.dispatch(pending, lambda out: True, at(12, 30)) == []


# 19
def test_night_shift_no_night_send_and_no_double_in_morning(bot):
    bot.presented(at(21, 50))
    assert bot.tick(at(22, 20)) == [] and bot.tick(at(3, day=13)) == []
    assert bodies(bot.tick(at(9, 0, day=13))) == [R.REMINDER_NEUTRAL]
    assert bot.tick(at(9, 1, day=13)) == [] and bot.tick(at(9, 59, day=13)) == []
    assert bodies(bot.tick(at(10, 0, day=13))) == [R.DISCOUNT_1]


# 20
def test_callback_time_is_respected(bot):
    bot.presented(at(12))
    bot.say("напишите завтра после обеда", at(12), intent="not_ready", callback_at="2026-10-13 14:00")
    assert bot.tick(at(13)) == [] and bot.tick(at(13, 59, day=13)) == []
    assert bodies(bot.tick(at(14, 0, day=13))) == [R.REMINDER_NEUTRAL]


# 21
def test_silence_after_1290_does_not_drop_to_1190(bot):
    bot.presented(at(12))
    assert bodies(bot.say("дорого", at(12), intent="price_objection")) == [R.DISCOUNT_1]
    assert bodies(bot.tick(at(12, 30))) == [R.REMINDER_NEUTRAL]
    assert bot.tick(at(13, 30)) == [] and bot.tick(at(18)) == []
    assert bot.mem.price.offered == 1290


def test_discount_ladder_only_on_price_objection_and_never_below_1190(bot):
    bot.presented()
    bot.say("дорого", at(12), intent="price_objection")
    assert bodies(bot.say("а дешевле?", at(12), intent="price_objection")) == [R.DISCOUNT_2]
    assert bodies(bot.say("ещё дешевле", at(12), intent="price_objection")) == [R.DISCOUNT_FLOOR]
    bot.say("ладно, оформляйте", at(12), intent="order_now", offer_reply="accept")
    assert bot.mem.price.agreed == 1190


def test_consent_at_full_price_gets_no_discount(bot):
    bot.in_checkout()
    assert bot.mem.price.agreed == 1490 and bot.mem.price.discount_level == 0


# 22
def test_address_phone_receipt_in_separate_messages(bot):
    bot.in_checkout()
    outs = bot.say("ул. Ленина 5", at(12), intent="order_data", order_data=OrderData(address="ул. Ленина 5"))
    assert "адрес" not in bodies(outs)[0] and "номер телефона" in bodies(outs)[0]
    bot.say("0555123456", at(12), intent="order_data", order_data=OrderData(phone="0555123456"))
    bot.brain.image = ImageCheck(kind="receipt", amount=1490, recipient_ok=True, date_ok=True)
    outs = bot.msg(Incoming(id="r1", kind="image"), at(12))
    assert bodies(outs) == [R.ORDER_DONE_PAID]
    card = bot.sink.cards[-1]
    assert (card["address"], card["phone"], card["price"], card["receipt"]) == ("ул. Ленина 5", "0555123456", 1490, "check_ok")


def test_manager_number_is_not_taken_as_client_phone(bot):
    bot.in_checkout()
    bot.say("0999000845", at(12), intent="order_data", order_data=OrderData(phone="0999 000 845"))
    assert bot.mem.order.phone == ""


# 23
def test_cash_on_delivery_needs_no_receipt(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    bot.say("Бишкек", at(12), geo=BISHKEK)
    outs = bot.say("Беру, оплата при получении", at(12), intent="order_now", order_data=OrderData(payment="cod"))
    assert "qr" not in kinds(outs) and "чек" not in bodies(outs)[-1]
    outs = bot.say("мкр 7, дом 1, 0555123456", at(12), intent="order_data",
                   order_data=OrderData(address="мкр 7, дом 1", phone="0555123456"))
    assert bodies(outs) == [R.ORDER_DONE_COD]


def test_cod_is_not_offered_in_regions(bot):
    bot.offered()
    bot.say("Беру, оплата при получении", at(12), intent="order_now", offer_reply="accept", order_data=OrderData(payment="cod"))
    assert bot.mem.order.payment == "full"


# 24
def test_teeth_photo_creates_no_order(bot):
    bot.presented()
    bot.brain.image = ImageCheck(kind="other")
    assert bot.msg(Incoming(id="p1", kind="image"), at(12)) == []
    assert bot.mem.order.status == "none" and bot.store.orders(CHAT) == []


# 25
@pytest.mark.parametrize("check, note", [
    (ImageCheck(kind="unreadable"), "автопроверка недоступна"),
    (ImageCheck(kind="receipt", amount=1000, recipient_ok=True, date_ok=True), "сумма 1000 вместо 1490"),
])
def test_receipt_problem_goes_to_manual_review(bot, check, note):
    bot.in_checkout()
    bot.say("адрес и телефон", at(12), intent="order_data", order_data=OrderData(address="ул. 1", phone="0555123456"))
    bot.brain.image = check
    assert bodies(bot.msg(Incoming(id="r1", kind="image"), at(12))) == [R.ORDER_MANUAL_REVIEW]
    assert bot.sink.cards[-1]["receipt"] == "manual_review" and note in bot.sink.cards[-1]["receipt_note"]


# 26
def test_group_send_failure_keeps_order_and_retries(bot):
    bot.in_checkout()
    bot.say("адрес и телефон", at(12), intent="order_data", order_data=OrderData(address="ул. 1", phone="0555123456"))
    bot.sink.ok = False
    bot.brain.image = ImageCheck(kind="receipt", amount=1490, recipient_ok=True, date_ok=True)
    outs = bot.msg(Incoming(id="r1", kind="image"), at(12))
    assert "передаю в доставку" not in bodies(outs)[0]
    assert bot.mem.order.status == "registered" and bot.mem.order.handoff == "failed"
    assert len(bot.store.orders(CHAT)) == 1
    bot.sink.ok = True
    bot.tick(at(12, 1))
    assert bot.mem.order.status == "handed_off" and len(bot.sink.cards) == 1


# 27
def test_repeated_receipt_creates_no_second_order(bot):
    bot.in_checkout()
    bot.say("адрес и телефон", at(12), intent="order_data", order_data=OrderData(address="ул. 1", phone="0555123456"))
    bot.brain.image = ImageCheck(kind="receipt", amount=1490, recipient_ok=True, date_ok=True)
    receipt = Incoming(id="r1", kind="image")
    bot.msg(receipt, at(12))
    bot.msg(receipt, at(12))
    bot.msg(Incoming(id="r2", kind="image"), at(12))
    assert len(bot.store.orders(CHAT)) == 1 and len(bot.sink.cards) == 1


# 28
def test_question_during_checkout_keeps_order_and_price(bot):
    bot.presented()
    bot.say("дорого", at(12), intent="price_objection")
    bot.say("хорошо, оформляйте", at(12), intent="order_now", offer_reply="accept")
    outs = bot.say("а сколько держать?", at(12), intent="question", questions=qs("usage_time"))
    assert bodies(outs)[0] == "ответ[checkout] на 1" and "чек об оплате" in bodies(outs)[1]
    m = bot.mem
    assert (m.order.status, m.price.agreed, m.sales.answered_in_cycle) == ("collecting", 1290, 0)


def test_checkout_reminder_asks_for_missing_and_keeps_price(bot):
    bot.in_checkout(at(12))
    outs = bot.tick(at(12, 30))
    assert "чек об оплате" in bodies(outs)[0] and R.REMINDER_NEUTRAL not in bodies(outs)
    assert bot.tick(at(14)) == [] and bot.mem.price.discount_level == 0


# 29
def test_manager_takeover_stops_bot_and_reminders(bot):
    bot.presented(at(12))
    bot.engine.operator_took_over(CHAT, at(12))
    assert bot.say("а цена?", at(12, 5), intent="question", questions=qs("price")) == []
    assert bot.tick(at(13)) == [] and bot.brain.write_calls == 0


# 30
def test_long_dialog_keeps_everything(bot):
    bot.presented()
    bot.say("дорого", at(12), intent="price_objection")
    bot.say("?", at(12), intent="question", questions=qs("a", "b"))
    bot.say("оформляйте, 2 коробки", at(12), intent="order_now", offer_reply="accept", order_data=OrderData(quantity=2))
    bot.say("вопрос", at(12), intent="question", questions=qs("usage"))
    bot.say("ул. 1", at(12), intent="order_data", order_data=OrderData(address="ул. 1"))
    m = bot.mem
    assert (m.geo.place, m.price.agreed, m.price.discount_level, m.order.quantity, m.order.address) == ("Ош", 1290, 1, 2, "ул. 1")
    assert m.sales.consent_text == "оформляйте, 2 коробки"


# Отдельные правила
def test_explicit_buyer_does_not_wait_for_three_questions(bot):
    assert bodies(bot.say("Хочу купить, оформляйте", at(12), intent="order_now")) == [R.PRESENTATION, R.CITY_QUESTION]
    outs = bot.say("Бишкек", at(12), geo=BISHKEK)
    assert kinds(outs) == ["text", "qr", "text"], "доставка, затем сразу оформление без фото и голосового"


def test_foreign_country_gets_own_price_and_manager_for_payment(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    outs = bot.say("Астана", at(12), geo=geo("kz_other", "Астана", "Астану"))
    assert bodies(outs)[:2] == ["Да, у нас есть доставка в Астану через СДЭК ✅", "Цена набора — 7900 тенге."]
    assert bot.say("дорого", at(12), intent="price_objection") == [], "лестница КР в тенге не переносится"
    bot.say("беру", at(12), intent="order_now")
    assert bot.mem.control.mode == "manager" and bot.sink.cards[-1]["type"] == "handoff"


def test_unsupported_country(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    assert bodies(bot.say("Германия", at(12), geo=geo("unsupported", "Германия"))) == [R.UNSUPPORTED_COUNTRY]


def test_adverse_reaction_stops_sales(bot):
    bot.offered(at(12))
    outs = bot.say("после полосок жжёт дёсны", at(12), intent="adverse_reaction")
    assert bodies(outs) == [R.ADVERSE_REACTION]
    assert bot.tick(at(14)) == [] and bot.mem.control.mode == "manager"


def test_unknown_answer_goes_to_manager(bot):
    bot.presented()
    bot.brain.unknown = [1]
    assert bot.say("есть гарантийный талон?", at(12), intent="question", questions=qs("other")) == []
    assert "нет подтверждённого ответа" in bot.sink.cards[-1]["reason"]


def test_stop_request_ends_sales_contact(bot):
    bot.presented(at(12))
    assert bot.say("не пишите мне", at(12), intent="stop_contact") == []
    assert bot.tick(at(13)) == []


def test_failed_voice_does_not_resend_whole_block(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    bot.fail_kinds = {"voice"}
    assert kinds(bot.say("Ош", at(12), geo=OSH)) == ["text", "photo"]
    bot.fail_kinds = set()
    assert kinds(bot.say("ок", at(12), intent="smalltalk")) == ["voice"]


def test_unreadable_voice_goes_to_manager_without_reply(bot):
    bot.presented()
    assert bot.say("", at(12), kind="voice") == []
    assert bot.mem.control.mode == "manager"


def test_delivery_text_never_leaks_placeholder():
    assert R.delivery_text("kg_other", "") is None and R.delivery_text("kg_other", "[город]") is None


def test_final_decline_is_acknowledged_and_stops_reminders(bot):
    bot.offered(at(12))
    assert bodies(bot.say("Не надо оформлять", at(12), intent="cancel_order", offer_reply="decline_final")) == [R.NOT_READY_ACK]
    assert bot.tick(at(13)) == [] and bot.mem.order.status == "none"


def test_callback_request_alone_gets_short_ack(bot):
    bot.presented(at(12))
    assert bodies(bot.say("Напишите завтра после обеда", at(12), intent="other", callback_at="2026-10-13 14:00")) == [R.NOT_READY_ACK]


def test_quantity_written_as_words_is_parsed():
    assert OrderData.model_validate({"quantity": "1 коробка"}).quantity == 1
    assert OrderData.model_validate({"quantity": ""}).quantity is None


def test_price_question_in_first_message_is_closed_by_presentation(bot):
    outs = bot.say("Привет, цена?", at(12), intent="question", questions=qs("price"))
    assert bodies(outs) == [R.PRESENTATION, R.CITY_QUESTION] and bot.brain.write_calls == 0
    assert bot.mem.sales.answered_in_cycle == 1


def test_backing_out_during_checkout_stops_collecting(bot):
    bot.in_checkout(at(12))
    assert bodies(bot.say("не надо пока", at(12), intent="not_ready")) == [R.NOT_READY_ACK]
    assert bot.mem.order.status == "cancelled" and bot.mem.price.agreed is None


def test_expensive_during_checkout_gets_discount_then_checkout_at_new_price(bot):
    bot.in_checkout(at(12))
    assert bodies(bot.say("дорого", at(12), intent="price_objection")) == [R.DISCOUNT_1]
    outs = bot.say("ладно, беру", at(12), intent="order_now", offer_reply="accept")
    assert "чек об оплате" in bodies(outs)[-1] and bot.mem.price.agreed == 1290
    assert "qr" not in kinds(outs), "QR уже отправляли — повторно не уходит"


def test_agreeing_with_payment_choice_is_consent_not_condition(bot):
    bot.say("Здравствуйте", at(12), intent="greeting")
    bot.say("Токмок, хочу заказать", at(12), intent="order_now", geo=geo("tokmok", "Токмок"))
    bot.say("дорого", at(12), intent="price_objection")
    outs = bot.say("Ладно, беру, только оплата при получении", at(12), intent="order_now",
                   offer_reply="conditional", order_data=OrderData(payment="cod"))
    assert kinds(outs) == ["text"] and "адрес доставки" in bodies(outs)[0], "без фото, голосового и QR"
    m = bot.mem
    assert (m.order.status, m.order.payment, m.price.agreed) == ("collecting", "cod", 1290)


def test_documents_question_goes_to_manager_without_model_answer(bot):
    bot.presented()
    assert bot.say("есть сертификат?", at(12), intent="question", questions=qs("documents")) == []
    assert bot.brain.write_calls == 0 and bot.mem.control.mode == "manager"


def test_reply_with_internal_words_is_dropped(bot):
    bot.presented()
    bot.brain.write = lambda role, ctx: Written(messages=["В базе знаний этого нет"], answered=[1])
    assert bot.say("а гарантия?", at(12), intent="question", questions=qs("other")) == []
    assert bot.mem.control.mode == "manager" and bot.mem.sales.answered_in_cycle == 0


def test_plain_yes_becomes_daa(bot):
    bot.presented()
    bot.brain.write = lambda role, ctx: Written(messages=["Да", "Доступна оплата при получении"], answered=[1])
    assert bodies(bot.say("можно при получении?", at(12), intent="question", questions=qs("payment")))[0] == "Даа"


def test_several_questions_get_one_message_without_bare_yes():
    got = shape_bubbles(["В коробке 14 полосок.", "Держать 30 минут.", "Да", "В Бишкеке доступна оплата при получении."], 3)
    assert got == ["В коробке 14 полосок.\n\nДержать 30 минут.\n\nДа, в Бишкеке доступна оплата при получении."]


def test_single_yes_no_question_keeps_two_bubbles():
    assert shape_bubbles(["Да.", "Доступна оплата при получении"], 1) == ["Даа", "Доступна оплата при получении"]
    assert shape_bubbles(["Нет", "Продаём только целой коробкой"], 1) == ["Нет", "Продаём только целой коробкой"]
    assert shape_bubbles(["Полоски держат 30 минут."], 1) == ["Полоски держат 30 минут."]


def test_existing_client_of_old_bot_gets_no_second_presentation(bot):
    bot.engine.seed(CHAT, has_history=True, order_placed=False, now=at(12))
    assert bodies(bot.say("Здравствуйте", at(12), intent="greeting")) != [R.PRESENTATION, R.CITY_QUESTION]
    outs = bot.say("Ош", at(12), geo=OSH)
    assert kinds(outs) == ["text"] and "Ылдам" in bodies(outs)[0], "доставка без повторных фото и голосового"


def test_seed_does_not_touch_chat_the_engine_already_leads(bot):
    bot.presented()
    bot.engine.seed(CHAT, has_history=True, order_placed=True, now=at(12))
    assert bot.mem.order.status == "none"


def test_client_with_old_order_asking_status_goes_to_manager(bot):
    bot.engine.seed(CHAT, has_history=True, order_placed=True, now=at(12))
    assert bot.say("где мой заказ?", at(12), intent="order_status") == []
    assert bot.sink.cards[-1]["type"] == "handoff"


def test_failed_card_is_retried_on_tick(bot):
    bot.in_checkout()
    bot.say("адрес и телефон", at(12), intent="order_data", order_data=OrderData(address="ул. 1", phone="0555123456"))
    bot.brain.image = ImageCheck(kind="receipt", amount=1490, recipient_ok=True, date_ok=True)
    bot.msg(Incoming(id="r1", kind="image"), at(12))
    assert bot.sink.cards[-1]["receipt_message_id"] == "r1"
    bot.engine.card_failed(CHAT, "order", at(12))
    bot.tick(at(12, 1))
    assert len([c for c in bot.sink.cards if c["type"] == "order"]) == 2 and bot.mem.order.status == "handed_off"
    assert len(bot.store.orders(CHAT)) == 1


def test_message_ids_do_not_repeat_after_memory_reset(bot):
    first = bot.say("Здравствуйте", at(12), intent="greeting")
    bot.store.forget(CHAT)
    again = bot.msg(Incoming(id="fresh", text="Здравствуйте"), at(12), intent="greeting")
    assert bodies(first) == bodies(again) and not {o.id for o in first} & {o.id for o in again}


def test_address_without_checkout_is_saved_and_order_is_offered(bot):
    bot.engine.seed(CHAT, has_history=True, order_placed=False, now=at(12))
    outs = bot.say("Бишкек, ул. Ленина 5", at(12), intent="order_data", geo=BISHKEK, order_data=OrderData(address="ул. Ленина 5"))
    assert bodies(outs)[-1] == R.OFFER_FIRST
    outs = bot.say("да", at(12), intent="smalltalk", offer_reply="accept")
    assert "адрес доставки" not in bodies(outs)[-1] and "номер телефона" in bodies(outs)[-1]


def test_wish_to_buy_next_to_a_question_still_leads_to_checkout(bot):
    outs = bot.say("Здравствуйте. Хочу заказать. А в каком городе вы находитесь?", at(12), intent="question", buy_now=True, questions=qs("other"))
    assert bodies(outs)[0] == R.PRESENTATION and bodies(outs)[-1] == R.CITY_QUESTION and bot.mem.sales.order_intent_pending
    outs = bot.say("Бишкек, 6 мкр 7/1", at(12), intent="order_data", geo=BISHKEK, order_data=OrderData(address="6 мкр 7/1"))
    assert "qr" in kinds(outs) and "адрес доставки" not in bodies(outs)[-1]
    assert bot.mem.order.status == "collecting"


def test_promise_to_find_out_is_not_sent_to_client(bot):
    bot.presented()
    bot.brain.write = lambda role, ctx: Written(messages=["Точную стоимость уточню отдельно."], answered=[1])
    assert bot.say("сколько доставка в Москву?", at(12), intent="question", questions=qs("delivery_cost")) == []
    assert bot.mem.control.mode == "manager"


def test_fixed_texts_follow_client_language(bot):
    calls = []
    bot.brain.translate = lambda texts, language: calls.append((tuple(texts), language)) or [f"[ky] {text}" for text in texts]
    outs = bot.say("Баасы канча?", at(12), intent="question", language="ky", questions=qs("price"))
    assert outs[0].body.startswith("Салам!") and "1490" in outs[0].body, "утверждённый перевод презентации"
    assert outs[1].body == "Сураныч, кайсы шаардан болосуз?" and calls == []
    outs = bot.say("Ош", at(12), language="ky", geo=OSH)
    assert outs[0].body.startswith("[ky] Да, у нас есть доставка в Ош") and calls[-1][1] == "кыргызский"
    # перевод запомнен: второй раз модель не вызывается
    before = len(calls)
    bot.brain.translate = lambda texts, language: calls.append(1) or []
    assert bodies(bot.say("дорого", at(12), intent="price_objection", language="ky")) == [R.DISCOUNT_1], "перевод не удался — уходит русский"
    assert len(calls) == before + 1


def test_translation_that_changes_numbers_is_rejected(bot):
    bot.presented()
    bot.brain.translate = lambda texts, language: ["Сизге 1200 сомго сунуштайм" for _ in texts]
    assert bodies(bot.say("кымбат", at(12), intent="price_objection", language="ky")) == [R.DISCOUNT_1]


def test_model_answers_are_not_translated_again(bot):
    bot.presented()
    bot.brain.translate = lambda texts, language: [f"[ky] {text}" for text in texts]
    outs = bot.say("канча убакыт кармайм?", at(12), intent="question", language="ky", questions=qs("usage_time"))
    assert bodies(outs) == ["ответ[consult] на 1"]


def test_russian_client_gets_no_translation_calls(bot):
    bot.brain.translate = lambda texts, language: 1 / 0
    assert bodies(bot.say("Здравствуйте", at(12), intent="greeting")) == [R.PRESENTATION, R.CITY_QUESTION]


def test_translation_may_reorder_numbers(bot):
    bot.presented()
    moved = "Сизге 1490 сомдун ордуна 1290 сомго топтомду сунуштай алам"
    bot.brain.translate = lambda texts, language: [moved for _ in texts]
    assert bodies(bot.say("кымбат", at(12), intent="price_objection", language="ky")) == [moved]


def test_failed_reminder_is_not_retried_every_minute(bot):
    bot.presented(at(12))
    bot.fail_kinds = {"text"}
    assert bot.tick(at(12, 30)) == []
    assert bot.engine.tick(CHAT, at(12, 31)) == [] and bot.engine.tick(CHAT, at(12, 44)) == [], "после сбоя — пауза"
    bot.fail_kinds = set()
    assert bodies(bot.tick(at(12, 45))) == [R.REMINDER_NEUTRAL]


def test_client_message_clears_reminder_backoff(bot):
    bot.presented(at(12))
    bot.fail_kinds = {"text"}
    bot.tick(at(12, 30))
    bot.fail_kinds = set()
    bot.say("?", at(12, 31), intent="question", questions=qs("a"))
    assert bot.mem.wait.retry_after == 0


def test_uncertain_delivery_counts_as_sent_and_is_not_repeated(bot):
    bot.presented(at(12))
    out = bot.engine.tick(CHAT, at(12, 30))[0]
    bot.engine.delivered(out, True, at(12, 30), uncertain=True)
    assert bot.mem.wait.neutral_sent_at == at(12, 30)
    assert bot.tick(at(12, 31)) == [] and bot.tick(at(12, 45)) == []


def test_stale_reminder_is_dropped_not_sent(bot):
    bot.presented(at(12))
    assert bot.tick(at(12, day=13)) == [], "сутки спустя напоминание уже не уходит"
    assert bot.mem.wait.stopped is True
    assert bodies(bot.say("а цена?", at(13, day=13), intent="question", questions=qs("price")))
    assert bodies(bot.tick(at(13, 31, day=13))) == [R.REMINDER_NEUTRAL], "после нового сообщения цепочка работает снова"


def test_stop_all_reminders(bot):
    bot.presented(at(12))
    assert bot.engine.stop_all_reminders(at(12, 5)) == 1
    assert bot.tick(at(12, 30)) == [] and bot.tick(at(14)) == []


def test_ad_phrase_on_first_message_gets_only_the_script(bot):
    outs = bot.say("Здравствуйте! Можно узнать об этом подробнее?", at(12), intent="question", questions=qs("general"))
    assert bodies(outs) == [R.PRESENTATION, R.CITY_QUESTION] and bot.brain.write_calls == 0


def test_specific_question_on_first_message_is_answered_before_city_question(bot):
    outs = bot.say("Здравствуйте, а брекеты не мешают?", at(12), intent="question", questions=qs("safety"))
    assert bodies(outs) == [R.PRESENTATION, "ответ[consult] на 1", R.CITY_QUESTION]
