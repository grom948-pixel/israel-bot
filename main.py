"""
Сервер + Telegram бот для покупок в Израиле.
Агент ведёт живой диалог на иврите.
"""

import os, re, json, logging, asyncio, requests
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
SERVER_URL     = os.environ["SERVER_URL"]

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
fastapi_app = FastAPI()

calls: dict = {}
state: dict = {}
tg_app: Application = None


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
    prompt = (
        f"Ты агент для покупок в Израиле. Задача: \"{task}\"\n\n"
        "Создай первую фразу звонка на иврите и резюме по-русски.\n"
        "JSON без markdown: {\"opening\": \"שלום...\", \"summary\": \"Агент позвонит и...\"}"
    )
    raw = ask_claude([{"role": "user", "content": prompt}])
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def get_next_response(task: str, history: list) -> str:
    system = (
        f"Ты телефонный агент в Израиле. Задача: {task}\n"
        "Говори ТОЛЬКО на иврите. Короткие фразы (1-2 предложения).\n"
        "Торгуйся, уточняй детали, будь вежлив но настойчив.\n"
        "Когда задача выполнена — добавь слово КОНЕЦ в конце ответа."
    )
    return ask_claude(history, system)


def summarize_call(task: str, history: list) -> str:
    convo = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    prompt = f"Задача была: {task}\n\nРазговор:\n{convo}\n\nНапиши резюме по-русски: что удалось узнать/договориться?"
    return ask_claude([{"role": "user", "content": prompt}])


# ══════════════════════════════════════════════════
#  TWILIO WEBHOOKS
# ══════════════════════════════════════════════════

@fastapi_app.post("/call/start", response_class=PlainTextResponse)
async def call_start(CallSid: str = Form(default="")):
    call_data = calls.get(CallSid, {})
    opening = call_data.get("opening", "שלום, אני מתקשר.")
    if CallSid in calls:
        calls[CallSid]["history"].append({"role": "assistant", "content": opening})

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        language="he-IL",
        action=f"{SERVER_URL}/call/respond?sid={CallSid}",
        timeout=6,
        speech_timeout="auto",
    )
    gather.say(opening, language="he-IL", voice="Polly.Dina")
    vr.append(gather)
    vr.redirect(f"{SERVER_URL}/call/no-input?sid={CallSid}")
    return str(vr)


@fastapi_app.post("/call/respond", response_class=PlainTextResponse)
async def call_respond(sid: str = "", SpeechResult: str = Form(default="")):
    call_data = calls.get(sid)
    if not call_data:
        vr = VoiceResponse()
        vr.say("תודה, שלום.", language="he-IL", voice="Polly.Dina")
        vr.hangup()
        return str(vr)

    vr = VoiceResponse()

    if SpeechResult:
        call_data["history"].append({"role": "user", "content": SpeechResult})
        next_reply = get_next_response(call_data["task"], call_data["history"])
        finished = "КОНЕЦ" in next_reply
        clean = next_reply.replace("КОНЕЦ", "").strip()
        call_data["history"].append({"role": "assistant", "content": clean})

        if finished or len(call_data["history"]) > 20:
            vr.say(clean, language="he-IL", voice="Polly.Dina")
            vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
            vr.hangup()
            asyncio.create_task(finish_call(sid, call_data))
        else:
            gather = Gather(
                input="speech",
                language="he-IL",
                action=f"{SERVER_URL}/call/respond?sid={sid}",
                timeout=6,
                speech_timeout="auto",
            )
            gather.say(clean, language="he-IL", voice="Polly.Dina")
            vr.append(gather)
            vr.redirect(f"{SERVER_URL}/call/no-input?sid={sid}")
    else:
        vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
        vr.hangup()
        asyncio.create_task(finish_call(sid, call_data))

    return str(vr)


@fastapi_app.post("/call/no-input", response_class=PlainTextResponse)
async def call_no_input(sid: str = ""):
    call_data = calls.get(sid)
    vr = VoiceResponse()
    vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
    vr.hangup()
    if call_data:
        asyncio.create_task(finish_call(sid, call_data))
    return str(vr)


async def finish_call(sid: str, call_data: dict):
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
        log.error("Ошибка завершения: %s", e)


# ══════════════════════════════════════════════════
#  TELEGRAM BOT
# ══════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇮🇱 Привет! Я веду живые переговоры с израильскими магазинами.\n\n"
        "Напиши задачу и номер телефона — позвоню и буду торговаться на иврите!\n\n"
        "📌 Примеры:\n"
        "• «Позвони и спроси о собаках у Моше. Тел: 054-664-1812»\n"
        "• «Узнай цену на iPhone в магазине. Номер: 03-1234567»"
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
        await start_call_flow(update, user_id, chat_id)
        return

    phone, task = extract_phone(text)
    if not task:
        await update.message.reply_text("Напиши что нужно сделать 🙂")
        return

    state[user_id] = {"task": task, "phone": phone}
    if phone:
        await start_call_flow(update, user_id, chat_id)
    else:
        await update.message.reply_text(
            f"📋 Задача: *{task}*\n\n📞 Напиши номер телефона:",
            parse_mode="Markdown",
        )
        state[user_id]["waiting_phone"] = True


async def start_call_flow(update: Update, user_id: int, chat_id: int):
    s = state[user_id]
    msg = await update.message.reply_text("🔄 Подготавливаю агента...")
    try:
        translated = translate_task(s["task"])
        state[user_id]["opening"] = translated["opening"]
        state[user_id]["chat_id"] = chat_id

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Позвонить и вести диалог!", callback_data="call_confirm")],
            [InlineKeyboardButton("✏️ Изменить задачу", callback_data="call_change")],
        ])
        await msg.edit_text(
            f"📋 *Задача агента:*\n{translated['summary']}\n\n"
            f"🇮🇱 *Первая фраза:*\n{translated['opening']}\n\n"
            f"📞 Номер: `{s['phone']}`",
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

            call = twilio_client.calls.create(
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
            }
            await ctx.bot.send_message(
                chat_id=chat_id,
                text="📞 *Звонок начат!*\nАгент ведёт диалог на иврите.\nКогда закончится — пришлю итог по-русски 🇷🇺",
                parse_mode="Markdown",
            )
        except Exception as e:
            await ctx.bot.send_message(chat_id, f"❌ Ошибка: {e}")

    elif query.data == "call_change":
        state.pop(user_id, None)
        await query.edit_message_text("Хорошо, напиши задачу заново 👇")


# ══════════════════════════════════════════════════
#  ЗАПУСК — FastAPI + Telegram в одном event loop
# ══════════════════════════════════════════════════

async def run_telegram():
    global tg_app
    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram бот запущен ✅")


@fastapi_app.on_event("startup")
async def startup():
    asyncio.create_task(run_telegram())
    log.info("FastAPI запущен ✅")


@fastapi_app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
