#!/usr/bin/env python3
"""Локальный CLI-чат с ДУБЛЁМ бота Smilekit — не трогает продакшн (нет
Telegram-токена, нет Green API, нет записи в CRM/Supabase). Только вызывает
ту же модель (OpenAI) с изменённым промптом из этого дубля.

Запуск:
    /Users/graz1p/Desktop/SmilekitProject/SmileKit/bot/.venv/bin/python3 chat_cli.py

Команды в чате:
    /reset  — начать диалог заново
    /exit   — выйти
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from smilekit_bot.models import ChatMessage
from smilekit_bot.service import SmilekitAssistant


async def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY не найден в .env")
        return
    model = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
    assistant = SmilekitAssistant(
        api_key=api_key,
        model=model,
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )

    history: list[ChatMessage] = []
    memory = ""
    print(f"Тестовый бот Smilekit (дубль, модель {model}). /reset — сброс, /exit — выход.\n")

    while True:
        try:
            text = input("Клиент: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/exit":
            break
        if text == "/reset":
            history = []
            memory = ""
            print("— диалог сброшен —\n")
            continue

        reply = await assistant.reply(text, history, customer_memory=memory)
        print(f"Бот: {reply.text}\n")
        if reply.supervisor.customer_memory:
            memory = reply.supervisor.customer_memory
        history.append(ChatMessage(role="user", content=text))
        history.append(ChatMessage(role="assistant", content=reply.text))


if __name__ == "__main__":
    asyncio.run(main())
