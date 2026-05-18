"""
Быстрая проверка подключения к Groq API (Llama 3.3 70B).

Запуск:
    python check_groq.py
"""

import os
import sys
from pathlib import Path

# Загружаем .env если есть
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    print("❌  GROQ_API_KEY не найден.")
    print("    1. Зарегистрируйся на https://console.groq.com")
    print("    2. Создай API ключ: API Keys → Create API Key")
    print("    3. Добавь в файл .env:")
    print("       GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx")
    sys.exit(1)

try:
    from groq import Groq
except ImportError:
    print("❌  Пакет groq не установлен. Запусти:")
    print("    pip install groq")
    sys.exit(1)

client = Groq(api_key=api_key)

print("Подключаемся к Groq → Llama 3.3 70B...")
print("─" * 50)

response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[
        {
            "role": "system",
            "content": (
                "Ты SQL-эксперт по PostgreSQL. "
                "Отвечай только SQL-запросом, без объяснений."
            ),
        },
        {
            "role": "user",
            "content": (
                "Напиши SQL-запрос: выбрать топ-5 сотрудников "
                "из таблицы sys_employee со статусом 1, "
                "отсортированных по фамилии. Вернуть: id, name, sur_name."
            ),
        },
    ],
    temperature=0.1,
    max_tokens=256,
)

sql = response.choices[0].message.content.strip()
usage = response.usage

print(sql)
print("─" * 50)
print(f"✓  Модель:          {response.model}")
print(f"   Токены запроса:  {usage.prompt_tokens}")
print(f"   Токены ответа:   {usage.completion_tokens}")
print(f"   Итого токенов:   {usage.total_tokens}")
print("\n✅  Groq API работает. Можно строить Generator и Auditor.")
