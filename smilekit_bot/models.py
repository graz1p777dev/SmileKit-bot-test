from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CrmAction(BaseModel):
    stage: str = ""
    tags: list[str] = Field(default_factory=list)
    note: str = ""


class OrderDetails(BaseModel):
    """Структурированный заказ для CRM.

    Всё опционально и по умолчанию None: менеджеру пустое поле дешевле
    выдуманного. Модель обязана вернуть null, если клиент этого не называл.
    """

    quantity: int | None = None
    total_amount: float | None = None
    currency: str | None = None
    city: str | None = None
    address: str | None = None
    customer_name: str | None = None
    contact_phone: str | None = None
    contact_username: str | None = None


class SupervisorResult(BaseModel):
    status: Literal["approved", "rewrite"]
    risk_level: Literal["low", "medium", "high"]
    issues: list[str] = Field(default_factory=list)
    corrected_response: str = ""
    handoff_reason: str = ""
    crm_action: CrmAction = Field(default_factory=CrmAction)
    customer_memory: str = ""
    order_summary: str = ""
    order: OrderDetails = Field(default_factory=OrderDetails)
    # Короткая заметка себе же: к чему относится короткое "да/давайте" клиента,
    # если он ответит именно так на ПОСЛЕДНЕЕ сообщение бота — подставляется в
    # следующий вызов (см. МЕНЕДЖЕР/history), чтобы модель не путала короткое
    # согласие с более ранней темой диалога (например, заказ вместо состава).
    expected_client_reply: str = ""


class AssistantReply(BaseModel):
    text: str
    supervisor: SupervisorResult
