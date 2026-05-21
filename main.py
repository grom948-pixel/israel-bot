"""
Сервер + Telegram бот для покупок в Израиле.
Агент ведёт живой диалог на иврите через Twilio + Claude.
"""

import os, re, json, logging, asyncio, requests
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
from fastapi import FastAPI, Form, Request
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
SERVER_URL     = os.environ["SERVER_URL"].rstrip("/")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
fastapi_app = FastAPI()

# Хранилище: используем номер телефона как ключ (до получения call_sid)
calls: dict = {}          # call_sid -> data
pending: dict = {}        # phone -> data (до звонка)
state: dict = {}          # telegram user_id -> state
tg_app: Application = None


# ══════════════════════════════════════════════════
#  CLAUDE
# ══════════════════════════════════════════════════

def ask_claude(messages: list, system: str = "") -> str:
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
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
        timeout=25,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def translate_task(task: str) -> dict:
    prompt = (
        f"Ты агент для покупок в Израиле. Задача клиента: \"{task}\"\n\n"
        "Создай первую фразу звонка на иврите (представься и объясни цель) "
        "и краткое резюме по-русски.\n"
        "Ответь ТОЛЬКО JSON без markdown:\n"
        "{\"opening\": \"שלום...\", \"summary\": \"Агент позвонит и...\"}"
    )
    raw = ask_claude([{"role": "user", "content": prompt}])
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def get_next_reply(task: str, history: list) -> str:
    system = (
        f"Ты телефонный агент в Израиле. Твоя задача: {task}\n"
        "ВАЖНО: говори ТОЛЬКО на иврите. Максимум 2 предложения.\n"
        "Торгуйся, уточняй, будь вежлив но настойчив.\n"
        "Когда задача выполнена или разговор закончен — добавь ##КОНЕЦ## в конце."
    )
    return ask_claude(history, system)


def summarize(task: str, history: list) -> str:
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    prompt = (
        f"Задача была: {task}\n\nРазговор:\n{convo}\n\n"
        "Напиши резюме по-русски: что узнали / о чём договорились / что купили?"
    )
    return ask_claude([{"role": "user", "content": prompt}])


# ══════════════════════════════════════════════════
#  TWILIO WEBHOOKS
# ══════════════════════════════════════════════════

@fastapi_app.post("/call/start", response_class=PlainTextResponse)
async def call_start(request: Request):
    form = await request.form()
    CallSid = form.get("CallSid", "")
    To = form.get("To", "")
    log.info(f"call/start: CallSid={CallSid} To={To}")

    # Ищем данные по номеру телефона
    call_data = None
    for phone_key, data in list(pending.items()):
        if To and To.replace(" ", "") in phone_key.replace(" ", ""):
            call_data = data
            pending.pop(phone_key, None)
            break

    if not call_data and pending:
        # Берём последний pending если не нашли по номеру
        phone_key = list(pending.keys())[-1]
        call_data = pending.pop(phone_key)

    if call_data:
        calls[CallSid] = call_data
        opening = call_data.get("opening", "שלום, אני מתקשר.")
        calls[CallSid]["history"] = [{"role": "assistant", "content": opening}]
    else:
        opening = "שלום, אני מתקשר בשמך."

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
    vr.redirect(f"{SERVER_URL}/call/done?sid={CallSid}")
    return str(vr)


@fastapi_app.post("/call/respond", response_class=PlainTextResponse)
async def call_respond(request: Request):
    form = await request.form()
    sid = request.query_params.get("sid", "")
    SpeechResult = form.get("SpeechResult", "")
    log.info(f"call/respond: sid={sid} speech={SpeechResult[:50] if SpeechResult else '(пусто)'}")
    call_data = calls.get(sid)
    vr = VoiceResponse()

    if not call_data or not SpeechResult:
        vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
        vr.hangup()
        if call_data:
            asyncio.create_task(send_summary(sid, call_data))
        return str(vr)

    call_data["history"].append({"role": "user", "content": SpeechResult})

    try:
        reply = get_next_reply(call_data["task"], call_data["history"])
    except Exception as e:
        log.error(f"Claude error: {e}")
        reply = "סליחה, לא הבנתי. תודה, שלום! ##КОНЕЦ##"

    finished = "##КОНЕЦ##" in reply
    clean = reply.replace("##КОНЕЦ##", "").strip()
    call_data["history"].append({"role": "assistant", "content": clean})

    if finished or len(call_data["history"]) > 18:
        vr.say(clean, language="he-IL", voice="Polly.Dina")
        vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
        vr.hangup()
        asyncio.create_task(send_summary(sid, call_data))
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
        vr.redirect(f"{SERVER_URL}/call/done?sid={sid}")

    return str(vr)


@fastapi_app.post("/call/done", response_class=PlainTextResponse)
async def call_done(request: Request):
    sid = request.query_params.get("sid", "")
    call_data = calls.get(sid)
    vr = VoiceResponse()
    vr.say("תודה רבה, שלום!", language="he-IL", voice="Polly.Dina")
    vr.hangup()
    if call_data:
        asyncio.create_task(send_summary(sid, call_data))
    return str(vr)


async def send_summary(sid: str, call_data: dict):
    try:
        summary = summarize(call_data["task"], call_data["history"])
        if tg_app:
            await tg_app.bot.send_message(
                chat_id=call_data["chat_id"],
                text=f"✅ *Звонок завершён!*\n\n📋 *Итог по-русски:*\n{summary}",
                parse_mode="Markdown",
            )
        calls.pop(sid, None)
    except Exception as e:
        log.error(f"send_summary error: {e}")


# ══════════════════════════════════════════════════
#  TELEGRAM BOT
# ══════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇮🇱 Привет! Я веду живые переговоры с израильскими магазинами.\n\n"
        "Напиши задачу и номер — позвоню и буду торговаться на иврите!\n\n"
        "📌 Примеры:\n"
        "• «Позвони и спроси о собаках у Моше. Тел: 054-664-1812»\n"
        "• «Узнай цену на iPhone. Номер: 03-1234567»\n"
        "• «Закажи пиццу и узнай про скидки. Тел: 050-1234567»"
    )


def extract_phone(text: str):
    pattern = r"[\+\d][\d\s\-\(\)]{6,17}\d"
    match = re.search(pattern, text)
    if match:
        phone = match.group().strip()
        clean = (text[:match.start()] + " " + text[match.end():]).strip()
        for w in ["номер", "телефон", "тел", "tel", "phone", ":", "."]:
            clean = clean.replace(w, " ")
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
        await prepare_call(update, user_id, chat_id)
        return

    phone, task = extract_phone(text)
    if not task:
        await update.message.reply_text("Напиши что нужно сделать 🙂")
        return

    state[user_id] = {"task": task, "phone": phone, "chat_id": chat_id}
    if phone:
        await prepare_call(update, user_id, chat_id)
    else:
        await update.message.reply_text(
            f"📋 Задача: *{task}*\n\n📞 Напиши номер телефона:",
            parse_mode="Markdown",
        )
        state[user_id]["waiting_phone"] = True


async def prepare_call(update: Update, user_id: int, chat_id: int):
    s = state[user_id]
    msg = await update.message.reply_text("🔄 Подготавливаю агента...")
    try:
        translated = translate_task(s["task"])
        state[user_id]["opening"] = translated["opening"]

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Позвонить и вести диалог!", callback_data="call_confirm")],
            [InlineKeyboardButton("✏️ Изменить задачу", callback_data="call_change")],
        ])
        await msg.edit_text(
            f"📋 *Задача агента:*\n{translated['summary']}\n\n"
            f"🇮🇱 *Первая фраза на иврите:*\n{translated['opening']}\n\n"
            f"📞 Номер: `{s['phone']}`",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as e:
        log.error(f"prepare_call error: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "call_confirm":
        s = state.get(user_id, {})
        await query.edit_message_text("📞 Звоню... агент начнёт диалог на иврите!")
        try:
            phone = re.sub(r"[\s\-\(\)]", "", s["phone"])
            if phone.startswith("0"):
                phone = "+972" + phone[1:]
            elif not phone.startswith("+"):
                phone = "+" + phone

            # Сохраняем данные в pending ДО звонка
            pending[phone] = {
                "task": s["task"],
                "opening": s.get("opening", "שלום"),
                "history": [],
                "chat_id": chat_id,
            }

            call = twilio_client.calls.create(
                to=phone,
                from_=TWILIO_PHONE,
                url=f"{SERVER_URL}/call/start",
                method="POST",
            )
            log.info(f"Call created: {call.sid}")

            await ctx.bot.send_message(
                chat_id=chat_id,
                text="📞 *Звонок идёт!*\nАгент ведёт диалог на иврите.\nКогда закончится — пришлю итог по-русски 🇷🇺",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"call error: {e}")
            await ctx.bot.send_message(chat_id, f"❌ Ошибка звонка: {e}")

    elif query.data == "call_change":
        state.pop(user_id, None)
        await query.edit_message_text("Хорошо, напиши задачу заново 👇")


# ══════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════

@fastapi_app.on_event("startup")
async def startup():
    global tg_app
    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("✅ Telegram бот запущен")


@fastapi_app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
