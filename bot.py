"""
Бот для покупок в израильских магазинах.
Пишешь по-русски — он звонит на иврите.
"""

import os
import re
import json
import logging
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
BLAND_KEY      = os.environ["BLAND_KEY"]

state: dict = {}


def ask_claude(prompt: str) -> str:
    """Прямой вызов Anthropic API через requests — без SDK."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇮🇱 Привет! Я делаю покупки в израильских магазинах.\n\n"
        "Просто напиши мне что нужно — я позвоню на иврите и сообщу результат.\n\n"
        "📌 Примеры:\n"
        "• «Позвони в Супер-Фарм, спроси есть ли ибупрофен. Тел: 03-6066777»\n"
        "• «Закажи доставку пиццы в Домино. Номер 03-1234567»\n\n"
        "/help — помощь"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Как пользоваться:\n\n"
        "1. Напиши запрос по-русски\n"
        "2. Укажи номер телефона магазина\n"
        "3. Проверь что скажет агент\n"
        "4. Нажми ✅ Позвонить\n"
        "5. Получи результат звонка\n\n"
        "/start — начать заново"
    )


def extract_phone(text: str):
    pattern = r"[\+\d][\d\s\-\(\)]{6,17}\d"
    match = re.search(pattern, text)
    if match:
        phone = match.group().strip()
        clean = text[:match.start()].strip() + " " + text[match.end():].strip()
        for word in ["номер", "телефон", "тел", "tel", "phone", ":", "."]:
            clean = clean.replace(word, " ").strip()
        clean = re.sub(r"\s+", " ", clean).strip()
        return phone, clean
    return None, text


async def translate_and_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_id: int):
    s = state[user_id]
    msg = await update.message.reply_text("🔄 Перевожу на иврит...")

    try:
        prompt = (
            f"Ты переводчик для шопинг-агента в Израиле.\n\n"
            f"Запрос клиента: \"{s['request']}\"\n\n"
            "Создай вежливый скрипт телефонного звонка на иврите и краткое резюме по-русски.\n\n"
            "Ответь СТРОГО в JSON без markdown:\n"
            "{\"hebrew_script\": \"שלום...\", \"russian_summary\": \"Агент позвонит и...\"}"
        )
        raw = ask_claude(prompt)
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)

        state[user_id]["hebrew_script"]  = parsed["hebrew_script"]
        state[user_id]["russian_summary"] = parsed["russian_summary"]

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Позвонить!", callback_data="call_confirm")],
            [InlineKeyboardButton("✏️ Изменить запрос", callback_data="call_change")],
        ])

        await msg.edit_text(
            f"📋 *Что скажет агент:*\n{parsed['russian_summary']}\n\n"
            f"🇮🇱 *Скрипт на иврите:*\n{parsed['hebrew_script']}\n\n"
            f"📞 Номер: `{s['phone']}`",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except json.JSONDecodeError:
        await msg.edit_text("❌ Ошибка формата. Попробуй ещё раз.")
    except Exception as e:
        log.error("Ошибка перевода: %s", e)
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if state.get(user_id, {}).get("waiting_phone"):
        state[user_id]["waiting_phone"] = False
        state[user_id]["phone"] = text.strip()
        await translate_and_preview(update, ctx, user_id)
        return

    phone, request = extract_phone(text)
    if not request:
        await update.message.reply_text("Напиши что нужно сделать 🙂")
        return

    state[user_id] = {"request": request, "phone": phone}

    if phone:
        await translate_and_preview(update, ctx, user_id)
    else:
        await update.message.reply_text(
            f"📋 Понял: *{request}*\n\n📞 Напиши номер телефона магазина:",
            parse_mode="Markdown",
        )
        state[user_id]["waiting_phone"] = True


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "call_confirm":
        s = state.get(user_id, {})
        if not s.get("hebrew_script"):
            await query.edit_message_text("❌ Данные устарели, начни заново.")
            return

        await query.edit_message_text("📞 Звоню в магазин...")

        try:
            phone = re.sub(r"[\s\-\(\)]", "", s["phone"])
            if phone.startswith("0"):
                phone = "+972" + phone[1:]
            elif not phone.startswith("+"):
                phone = "+" + phone

            resp = requests.post(
                "https://api.bland.ai/v1/calls",
                headers={"Authorization": BLAND_KEY, "Content-Type": "application/json"},
                json={
                    "phone_number": phone,
                    "task": s["hebrew_script"],
                    "language": "HEB",
                    "voice": "nat",
                    "max_duration": 10,
                    "record": True,
                },
                timeout=15,
            )
            data = resp.json()

            if data.get("call_id"):
                state[user_id]["call_id"] = data["call_id"]
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Проверить результат", callback_data="check_status")]
                ])
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"✅ *Звонок начат!*\n"
                        f"ID: `{data['call_id']}`\n\n"
                        "Нажми кнопку через 2–3 минуты чтобы узнать результат."
                    ),
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            else:
                await ctx.bot.send_message(
                    query.message.chat_id,
                    f"❌ Ошибка: {data.get('message', str(data))}"
                )

        except Exception as e:
            log.error("Ошибка звонка: %s", e)
            await ctx.bot.send_message(query.message.chat_id, f"❌ Ошибка звонка: {e}")

    elif query.data == "check_status":
        call_id = state.get(user_id, {}).get("call_id")
        if not call_id:
            await query.edit_message_text("❌ ID звонка не найден.")
            return

        try:
            resp = requests.get(
                f"https://api.bland.ai/v1/calls/{call_id}",
                headers={"Authorization": BLAND_KEY},
                timeout=10,
            )
            data = resp.json()
            status_ru = {
                "completed": "Завершён ✅",
                "failed":    "Ошибка ❌",
                "active":    "Идёт... 📞",
                "queued":    "В очереди ⏳",
            }.get(data.get("status"), data.get("status", "?"))

            transcript = data.get("concatenated_transcript", "")
            if not transcript and data.get("transcripts"):
                transcript = "\n".join(
                    f"{t.get('user','?')}: {t.get('text','')}"
                    for t in data["transcripts"]
                )

            text = f"📊 *Статус:* {status_ru}\n"
            if transcript:
                text += f"\n📝 *Транскрипт:*\n```\n{transcript[:1400]}\n```"
            else:
                text += "\nТранскрипт ещё не готов."

            still_active = data.get("status") in ("active", "queued")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="check_status")]
            ]) if still_active else None

            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка проверки: {e}")

    elif query.data == "call_change":
        state.pop(user_id, None)
        await query.edit_message_text("Хорошо, напиши запрос заново 👇")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
