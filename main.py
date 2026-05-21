import os, re, json, logging, threading, asyncio, requests
from flask import Flask, request as freq
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
TWILIO_SID     = os.environ["TWILIO_SID"]
TWILIO_TOKEN   = os.environ["TWILIO_TOKEN"]
TWILIO_PHONE   = os.environ["TWILIO_PHONE"]
SERVER_URL     = os.environ["SERVER_URL"].rstrip("/")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
flask_app = Flask(__name__)

pending = {}
calls   = {}
state   = {}
tg_loop = None
tg_app  = None

VOICE    = "Google.he-IL-Standard-B"
LANG     = "he-IL"


def ask_claude(messages, system=""):
    body = {"model": "claude-haiku-4-5-20251001", "max_tokens": 400, "messages": messages}
    if system:
        body["system"] = system
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json=body, timeout=25,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]

def translate_task(task):
    raw = ask_claude([{"role": "user", "content":
        f"Задача: \"{task}\"\nСоздай первую фразу звонка на иврите и резюме по-русски.\n"
        "JSON без markdown: {\"opening\": \"שלום...\", \"summary\": \"Агент...\"}"
    }])
    return json.loads(raw.replace("```json","").replace("```","").strip())

def next_reply(task, history):
    return ask_claude(history,
        f"Ты телефонный агент в Израиле. Задача: {task}\n"
        "Говори ТОЛЬКО на иврите. Максимум 2 предложения.\n"
        "Торгуйся и уточняй. Когда задача выполнена — добавь ##КОНЕЦ## в конце.")

def summarize(task, history):
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    return ask_claude([{"role":"user","content":
        f"Задача: {task}\nРазговор:\n{convo}\nНапиши резюме по-русски: что узнали/договорились?"}])


@flask_app.route("/call/start", methods=["POST"])
def call_start():
    sid = freq.form.get("CallSid","")
    to  = freq.form.get("To","").replace(" ","")
    log.info(f"call/start sid={sid} to={to}")

    data = None
    for key in list(pending.keys()):
        if to in key or key in to:
            data = pending.pop(key)
            break
    if not data and pending:
        data = pending.pop(list(pending.keys())[-1])

    if data:
        calls[sid] = {**data, "history": [{"role":"assistant","content":data["opening"]}]}
        opening = data["opening"]
    else:
        opening = "שלום, אני מתקשר."

    vr = VoiceResponse()
    g = Gather(input="speech", language=LANG,
               action=f"{SERVER_URL}/call/respond?sid={sid}",
               timeout=6, speech_timeout="auto")
    g.say(opening, language=LANG, voice=VOICE)
    vr.append(g)
    vr.redirect(f"{SERVER_URL}/call/done?sid={sid}")
    return str(vr), 200, {"Content-Type":"text/xml"}


@flask_app.route("/call/respond", methods=["POST"])
def call_respond():
    sid    = freq.args.get("sid","")
    speech = freq.form.get("SpeechResult","")
    data   = calls.get(sid)
    vr     = VoiceResponse()

    if not data or not speech:
        vr.say("תודה רבה, שלום!", language=LANG, voice=VOICE)
        vr.hangup()
        if data: _finish(sid, data)
        return str(vr), 200, {"Content-Type":"text/xml"}

    data["history"].append({"role":"user","content":speech})
    try:
        reply = next_reply(data["task"], data["history"])
    except Exception as e:
        log.error(e)
        reply = "תודה, שלום! ##КОНЕЦ##"

    done  = "##КОНЕЦ##" in reply
    clean = reply.replace("##КОНЕЦ##","").strip()
    data["history"].append({"role":"assistant","content":clean})

    if done or len(data["history"]) > 18:
        vr.say(clean, language=LANG, voice=VOICE)
        vr.say("תודה רבה, שלום!", language=LANG, voice=VOICE)
        vr.hangup()
        _finish(sid, data)
    else:
        g = Gather(input="speech", language=LANG,
                   action=f"{SERVER_URL}/call/respond?sid={sid}",
                   timeout=6, speech_timeout="auto")
        g.say(clean, language=LANG, voice=VOICE)
        vr.append(g)
        vr.redirect(f"{SERVER_URL}/call/done?sid={sid}")

    return str(vr), 200, {"Content-Type":"text/xml"}


@flask_app.route("/call/done", methods=["POST"])
def call_done():
    sid  = freq.args.get("sid","")
    data = calls.get(sid)
    vr   = VoiceResponse()
    vr.say("תודה רבה, שלום!", language=LANG, voice=VOICE)
    vr.hangup()
    if data: _finish(sid, data)
    return str(vr), 200, {"Content-Type":"text/xml"}


def _finish(sid, data):
    calls.pop(sid, None)
    def run():
        try:
            summary = summarize(data["task"], data["history"])
            if tg_loop and tg_app:
                asyncio.run_coroutine_threadsafe(
                    tg_app.bot.send_message(
                        chat_id=data["chat_id"],
                        text=f"✅ *Звонок завершён!*\n\n📋 *Итог:*\n{summary}",
                        parse_mode="Markdown",
                    ), tg_loop
                ).result(timeout=10)
        except Exception as e:
            log.error(f"finish error: {e}")
    threading.Thread(target=run, daemon=True).start()


@flask_app.route("/health")
def health():
    return "OK"


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇮🇱 Привет! Я веду живые переговоры с израильскими магазинами.\n\n"
        "Напиши задачу и номер — позвоню и буду торговаться на иврите!\n\n"
        "Примеры:\n"
        "• «Позвони и спроси о собаках. Тел: 054-664-1812»\n"
        "• «Узнай цену на iPhone. Номер: 03-1234567»"
    )

def extract_phone(text):
    m = re.search(r"[\+\d][\d\s\-\(\)]{6,17}\d", text)
    if m:
        phone = m.group().strip()
        clean = (text[:m.start()]+" "+text[m.end():]).strip()
        for w in ["номер","телефон","тел","tel","phone",":","."]:
            clean = clean.replace(w," ")
        return phone, re.sub(r"\s+"," ",clean).strip()
    return None, text

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    cid  = update.effective_chat.id
    text = update.message.text.strip()

    if state.get(uid,{}).get("waiting_phone"):
        state[uid]["waiting_phone"] = False
        state[uid]["phone"] = text.strip()
        await prepare(update, uid, cid)
        return

    phone, task = extract_phone(text)
    if not task:
        await update.message.reply_text("Напиши что нужно сделать 🙂")
        return

    state[uid] = {"task": task, "phone": phone, "chat_id": cid}
    if phone:
        await prepare(update, uid, cid)
    else:
        await update.message.reply_text(f"📋 Задача: *{task}*\n\n📞 Напиши номер:", parse_mode="Markdown")
        state[uid]["waiting_phone"] = True

async def prepare(update, uid, cid):
    s   = state[uid]
    msg = await update.message.reply_text("🔄 Подготавливаю агента...")
    try:
        t = translate_task(s["task"])
        state[uid]["opening"] = t["opening"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Позвонить!", callback_data="confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="change"),
        ]])
        await msg.edit_text(
            f"📋 *Задача:*\n{t['summary']}\n\n🇮🇱 *Первая фраза:*\n{t['opening']}\n\n📞 Номер: `{s['phone']}`",
            parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    cid = q.message.chat_id
    await q.answer()

    if q.data == "confirm":
        s = state.get(uid, {})
        await q.edit_message_text("📞 Звоню... агент говорит на иврите!")
        try:
            phone = re.sub(r"[\s\-\(\)]","",s.get("phone",""))
            if phone.startswith("0"): phone = "+972"+phone[1:]
            elif not phone.startswith("+"): phone = "+"+phone

            pending[phone] = {
                "task": s["task"],
                "opening": s.get("opening","שלום"),
                "history": [],
                "chat_id": cid,
            }
            call = twilio_client.calls.create(
                to=phone, from_=TWILIO_PHONE,
                url=f"{SERVER_URL}/call/start", method="POST"
            )
            log.info(f"Call: {call.sid}")
            await ctx.bot.send_message(
                cid,
                "📞 *Звонок идёт!*\nАгент говорит на иврите 🇮🇱\nКогда закончится — пришлю итог по-русски 🇷🇺",
                parse_mode="Markdown"
            )
        except Exception as e:
            await ctx.bot.send_message(cid, f"❌ Ошибка: {e}")

    elif q.data == "change":
        state.pop(uid, None)
        await q.edit_message_text("Напиши задачу заново 👇")


def start_telegram():
    global tg_loop, tg_app
    tg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(tg_loop)

    async def _run():
        global tg_app
        tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", cmd_start))
        tg_app.add_handler(CallbackQueryHandler(handle_callback))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        log.info("Telegram бот запущен ✅")
        async with tg_app:
            await tg_app.start()
            await tg_app.updater.start_polling(drop_pending_updates=True)
            await asyncio.Event().wait()

    tg_loop.run_until_complete(_run())


if __name__ == "__main__":
    t = threading.Thread(target=start_telegram, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
