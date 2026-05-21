"""
Бот для покупок в израильских магазинах.
Пишешь по-русски — он звонит на иврите через Twilio.
"""

import os
import re
import json
import logging
import requests
from twilio.rest import Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
TWILIO_SID     = os.environ["TWILIO_SID"]
TWILIO_TOKEN   = os.environ["TWILIO_TOKEN"]
TWILIO_PHONE   = os.environ["TWILIO_PHONE"]

twilio = Client(TWILIO_SID, TWILIO_TOKEN)
state: dict = {}


def ask_claude(prompt: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
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

            # TwiML — говорит текст и кладёт трубку
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="he-IL">{s['hebrew_script']}</Say>
</Response>"""

            call = twilio.calls.create(
                to=phone,
                from_=TWILIO_PHONE,
                twiml=twiml,
            )

            state[user_id]["call_sid"] = call.sid
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Проверить результат", callback_data="check_status")]
            ])
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    f"✅ *Звонок начат!*\n"
                    f"SID: `{call.sid}`\n\n"
                    "Нажми кнопку через 1–2 минуты чтобы узнать результат."
                ),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        except Exception as e:
            log.error("Ошибка звонка: %s", e)
            await ctx.bot.send_message(query.message.chat_id, f"❌ Ошибка звонка: {e}")

    elif query.data == "check_status":
        call_sid = state.get(user_id, {}).get("call_sid")
        if not call_sid:
            await query.edit_message_text("❌ ID звонка не найден.")
            return

        try:
            call = twilio.calls(call_sid).fetch()
            status_ru = {
                "completed": "Завершён ✅",
                "failed":    "Ошибка ❌",
                "in-progress": "Идёт... 📞",
                "queued":    "В очереди ⏳",
                "busy":      "Занято 📵",
                "no-answer": "Не ответили 📵",
            }.get(call.status, call.status)

            text = f"📊 *Статус звонка:* {status_ru}\n"
            text += f"⏱ Длительность: {call.duration} сек."

            still_active = call.status in ("in-progress", "queued", "ringing")
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
