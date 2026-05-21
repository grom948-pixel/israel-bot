"""
Сервер + Telegram бот для покупок в Израиле.
Агент ведёт живой диалог на иврите.
"""

import os, re, json, logging, threading, asyncio, requests
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse
import uvicorn

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
SERVER_URL     = os.environ["SERVER_URL"]  # https://твой-домен.railway.app

twilio = Client(TWILIO_SID, TWILIO_TOKEN)
app = FastAPI()

# Хранилище: call_sid -> { user_id, task, history, chat_id }
calls: dict = {}
# Хранилище состояний Telegram
state: dict = {}
# Telegram bot instance (глобальный)
tg_app = None


# ══════════════════════════════════════════════════
#  CLAUDE
# ══════════════════════════════════════════════════

def ask_claude(messages: list, system: str = "") -> str:
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "messages": messages,
    }
    if system:
        body["system"] = system

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def translate_task(task: str) -> dict:
    """Переводит задачу пользователя на иврит и создаёт первую фразу."""
    prompt = (
        f"Ты агент для покупок в Израиле. Задача: \"{task}\"\n\n"
        "Создай:\n"
        "1. Первую фразу звонка на иврите (представиться и объяснить цель)\n"
        "2. Краткое резюме по-русски что будет делать агент\n\n"
        "JSON без markdown: {\"opening\": \"שלום...\", \"summary\": \"Агент позвонит и...\"}"
    )
    raw = ask_claude([{"role": "user", "content": prompt}])
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def get_next_response(task: str, history: list, last_said: str) -> str:
    """Генерирует следующую реплику агента на иврите."""
    system = (
        f"Ты телефонный агент в Израиле. Твоя задача: {task}\n"
        "Говори ТОЛЬКО на иврите. Короткие фразы (1-2 предложения).\n"
        "Торгуйся, уточняй детали, будь вежлив но настойчив.\n"
        "Когда задача выполнена или разговор закончен — заверши словом КОНЕЦ в конце."
    )
    messages = history + [{"role": "user", "content": f"Собеседник сказал: {last_said}"}]
    return ask_claude(messages, system)


def summarize_call(task: str, history: list) -> str:
    """Резюмирует разговор по-русски."""
    convo = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    prompt = (
        f"Задача была: {task}\n\nРазговор:\n{convo}\n\n"
        "Напиши краткое резюме по-русски: что удалось узнать/договориться/купить?"
    )
    return ask_claude([{"role": "user", "content": prompt}])


# ══════════════════════════════════════════════════
#  TWILIO WEBHOOKS
# ══════════════════════════════════════════════════

@app.post("/call/start", response_class=PlainTextResponse)
async def call_start(request: Request, CallSid: str = Form(default="")):
    """Первый webhook — начало звонка."""
    call_data = calls.get(CallSid, {})
    opening = call_data.get("opening", "שלום, אני מתקשר בשמך.")

    # Добавляем в историю
    if CallSid in calls:
        calls[CallSid]["history"].append({"role": "assistant", "content": opening})

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        language="he-IL",
        action=f"{SERVER_URL}/call/respond?sid={CallSid}",
        timeout=5,
        speech_timeout="auto",
    )
    gather.say(opening, language="he-IL", voice="Polly.Dina")
    vr.append(gather)
    # Если не ответили
    vr.redirect(f"{SERVER_URL}/call/respond?sid={CallSid}&SpeechResult=")
    return str(vr)


@app.post("/call/respond", response_class=PlainTextResponse)
async def call_respond(
    request: Request,
    sid: str = "",
    SpeechResult: str = Form(default=""),
):
    """Webhook после каждого ответа собеседника."""
    call_data = calls.get(sid, {})
    if not call_data:
        vr = VoiceResponse()
        vr.say("תודה, שלום.", language="he-IL", voice="Polly.Dina")
        vr.hangup()
        return str(vr)

    # Добавляем реплику собеседника
    if SpeechResult:
        call_data["history"].append({"role": "user", "content": SpeechResult})

    vr = VoiceResponse()

    if not SpeechResult:
        # Тишина — прощаемся
        bye = "תודה רבה, שלום!"
        vr.say(bye, language="he-IL", voice="Polly.Dina")
        vr.hangup()
        await finish_call(sid, call_data)
        return str(vr)

    # Генерируем ответ
    next_reply = get_next_response(
        call_data["task"],
        call_data["history"],
        SpeechResult,
    )

    finished = "КОНЕЦ" in next_reply
    clean_reply = next_reply.replace("КОНЕЦ", "").strip()

    call_data["history"].append({"role": "assistant", "content": clean_reply})

    if finished or len(call_data["history"]) > 20:
        vr.say(clean_reply, language="he-IL", voice="Polly.Dina")
        vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
        vr.hangup()
        await finish_call(sid, call_data)
    else:
        gather = Gather(
            input="speech",
            language="he-IL",
            action=f"{SERVER_URL}/call/respond?sid={sid}",
            timeout=5,
            speech_timeout="auto",
        )
        gather.say(clean_reply, language="he-IL", voice="Polly.Dina")
        vr.append(gather)
        vr.redirect(f"{SERVER_URL}/call/respond?sid={sid}&SpeechResult=")

    return str(vr)


async def finish_call(sid: str, call_data: dict):
    """Отправляет резюме пользователю в Telegram."""
    try:
        summary = summarize_call(call_data["task"], call_data["history"])
        if tg_app:
            await tg_app.bot.send_message(
                chat_id=call_data["chat_id"],
                text=f"✅ *Звонок завершён!*\n\n📋 *Итог:*\n{summary}",
                parse_mode="Markdown",
            )
        calls.pop(sid, None)
    except Exception as e:
        log.error("Ошибка при завершении звонка: %s", e)


# ══════════════════════════════════════════════════
#  TELEGRAM BOT
# ══════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇮🇱 Привет! Я веду живые переговоры с израильскими магазинами.\n\n"
        "Напиши задачу и номер телефона — я позвоню и буду торговаться на иврите!\n\n"
        "📌 Примеры:\n"
        "• «Позвони и купи собаку у Моше. Тел: 054-664-1812»\n"
        "• «Узнай цену на ремонт iPhone в iFix. Номер: 03-1234567»\n"
        "• «Закажи доставку суши, узнай есть ли скидки. Тел: 050-1234567»"
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


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if state.get(user_id, {}).get("waiting_phone"):
        state[user_id]["waiting_phone"] = False
        state[user_id]["phone"] = text.strip()
        await start_call_flow(update, ctx, user_id, chat_id)
        return

    phone, task = extract_phone(text)
    if not task:
        await update.message.reply_text("Напиши что нужно сделать 🙂")
        return

    state[user_id] = {"task": task, "phone": phone}

    if phone:
        await start_call_flow(update, ctx, user_id, chat_id)
    else:
        await update.message.reply_text(
            f"📋 Задача: *{task}*\n\n📞 Напиши номер телефона:",
            parse_mode="Markdown",
        )
        state[user_id]["waiting_phone"] = True


async def start_call_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    s = state[user_id]
    msg = await update.message.reply_text("🔄 Подготавливаю агента...")

    try:
        translated = translate_task(s["task"])

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Позвонить и вести диалог!", callback_data="call_confirm")],
            [InlineKeyboardButton("✏️ Изменить задачу", callback_data="call_change")],
        ])

        state[user_id]["opening"] = translated["opening"]
        state[user_id]["chat_id"] = chat_id

        await msg.edit_text(
            f"📋 *Задача агента:*\n{translated['summary']}\n\n"
            f"🇮🇱 *Первая фраза на иврите:*\n{translated['opening']}\n\n"
            f"📞 Номер: `{s['phone']}`\n\n"
            "Агент будет вести полный диалог, торговаться и уточнять детали!",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "call_confirm":
        s = state.get(user_id, {})
        await query.edit_message_text("📞 Звоню... агент начнёт диалог!")

        try:
            phone = re.sub(r"[\s\-\(\)]", "", s["phone"])
            if phone.startswith("0"):
                phone = "+972" + phone[1:]
            elif not phone.startswith("+"):
                phone = "+" + phone

            call = twilio.calls.create(
                to=phone,
                from_=TWILIO_PHONE,
                url=f"{SERVER_URL}/call/start",
                method="POST",
            )

            calls[call.sid] = {
                "task": s["task"],
                "opening": s.get("opening", "שלום"),
                "history": [],
                "chat_id": chat_id,
                "user_id": user_id,
            }

            await ctx.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"📞 *Звонок начат!*\n"
                    f"Агент ведёт диалог на иврите.\n"
                    f"Когда разговор закончится — пришлю итог по-русски. 🇷🇺"
                ),
                parse_mode="Markdown",
            )

        except Exception as e:
            await ctx.bot.send_message(chat_id, f"❌ Ошибка: {e}")

    elif query.data == "call_change":
        state.pop(user_id, None)
        await query.edit_message_text("Хорошо, напиши задачу заново 👇")


# ══════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════

def run_bot():
    """Запускает Telegram бота в отдельном потоке."""
    global tg_app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram бот запущен ✅")
    tg_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Запускаем бота в фоне
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Запускаем FastAPI сервер
    port = int(os.environ.get("PORT", 8000))
    log.info(f"FastAPI сервер запущен на порту {port} ✅")
    uvicorn.run(app, host="0.0.0.0", port=port)
