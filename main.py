import os, re, json, logging, threading, asyncio, requests
from flask import Flask, request as freq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN       = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY        = os.environ["ANTHROPIC_KEY"]
BLAND_KEY            = os.environ["BLAND_KEY"]
BLAND_ENCRYPTED_KEY  = os.environ["BLAND_ENCRYPTED_KEY"]
TWILIO_PHONE         = os.environ["TWILIO_PHONE"]

flask_app = Flask(__name__)
state   = {}
tg_loop = None
tg_app  = None


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
        f"Задача: \"{task}\"\n"
        "Создай скрипт звонка на иврите (представься и объясни цель, 2-3 предложения) и резюме по-русски.\n"
        "JSON без markdown: {\"script\": \"שלום...\", \"summary\": \"Агент...\"}"
    }])
    return json.loads(raw.replace("```json","").replace("```","").strip())


def make_bland_call(phone, task_script, chat_id):
    """Звонит через Bland.ai используя зашифрованный Twilio ключ."""
    resp = requests.post(
        "https://api.bland.ai/v1/calls",
        headers={
            "Authorization": BLAND_KEY,
            "encrypted_key": BLAND_ENCRYPTED_KEY,
            "Content-Type": "application/json",
        },
        json={
            "phone_number": phone,
            "from": TWILIO_PHONE,
            "task": task_script,
            "language": "HEB",
            "voice": "nat",
            "max_duration": 10,
            "record": True,
            "webhook": f"https://worker-production-ad12.up.railway.app/bland/webhook?chat_id={chat_id}",
        },
        timeout=15,
    )
    return resp.json()


@flask_app.route("/bland/webhook", methods=["POST"])
def bland_webhook():
    """Bland.ai отправляет сюда результат после звонка."""
    chat_id = freq.args.get("chat_id", "")
    data = freq.json or {}
    log.info(f"Bland webhook: status={data.get('status')} chat_id={chat_id}")

    if data.get("status") == "completed" and chat_id:
        transcript = data.get("concatenated_transcript", "")
        task = data.get("variables", {}).get("task", "")

        def send_summary():
            try:
                if transcript:
                    summary = ask_claude([{"role": "user", "content":
                        f"Разговор завершён. Транскрипт:\n{transcript}\n\n"
                        "Напиши краткое резюме по-русски: что узнали, о чём договорились?"
                    }])
                else:
                    summary = "Транскрипт недоступен."

                asyncio.run_coroutine_threadsafe(
                    tg_app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"✅ *Звонок завершён!*\n\n📋 *Итог:*\n{summary}",
                        parse_mode="Markdown",
                    ), tg_loop
                ).result(timeout=10)
            except Exception as e:
                log.error(f"webhook summary error: {e}")

        threading.Thread(target=send_summary, daemon=True).start()

    return {"ok": True}


@flask_app.route("/health")
def health():
    return "OK"


# ── Telegram ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🇮🇱 Привет! Я веду живые переговоры с израильскими магазинами.\n\n"
        "Напиши задачу и номер — позвоню и буду говорить на иврите!\n\n"
        "Примеры:\n"
        "• «Позвони и спроси о собаках. Тел: 054-664-1812»\n"
        "• «Узнай цену на iPhone. Номер: 03-1234567»\n"
        "• «Закажи суши и узнай про скидки. Тел: 050-1234567»"
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
        state[uid]["script"] = t["script"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Позвонить!", callback_data="confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="change"),
        ]])
        await msg.edit_text(
            f"📋 *Задача:*\n{t['summary']}\n\n"
            f"🇮🇱 *Скрипт на иврите:*\n{t['script']}\n\n"
            f"📞 Номер: `{s['phone']}`",
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
        await q.edit_message_text("📞 Звоню через Bland.ai на иврите...")
        try:
            phone = re.sub(r"[\s\-\(\)]","",s["phone"])
            if phone.startswith("0"): phone = "+972"+phone[1:]
            elif not phone.startswith("+"): phone = "+"+phone

            result = make_bland_call(phone, s.get("script",""), cid)
            log.info(f"Bland call result: {result}")

            if result.get("call_id"):
                await ctx.bot.send_message(
                    cid,
                    f"📞 *Звонок начат!*\n"
                    f"ID: `{result['call_id']}`\n\n"
                    "Когда разговор закончится — пришлю итог по-русски 🇷🇺",
                    parse_mode="Markdown"
                )
            else:
                await ctx.bot.send_message(cid, f"❌ Ошибка: {result.get('message', str(result))}")
        except Exception as e:
            log.error(f"call error: {e}")
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
    log.info(f"Flask на порту {port} ✅")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
