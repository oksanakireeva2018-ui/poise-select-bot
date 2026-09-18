import os
import io
import time
import base64
import random
import json
import logging
from datetime import time as dtime

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("poise-select-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
YANDEX_API_KEY = os.environ["YANDEX_API_KEY"]
YANDEX_FOLDER_ID = os.environ["YANDEX_FOLDER_ID"]

YANDEX_TEXT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_IMAGE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
YANDEX_OPERATION_URL = "https://llm.api.cloud.yandex.net/operations/{op_id}"

STATE_FILE = "state.json"
ARCHIVE_FILE = "approved_drafts.log"

BRAND_PROMPT = """Ты — редактор канала POISE SELECT (Яндекс Дзен, дом и интерьер).

Роль бренда: личный куратор с очень хорошим вкусом, которому можно доверить выбор.
Формула голоса: «Я отобрала и объясню, почему это достойно внимания.»
Ключевая ценность: красивого слишком много — читателю не нужно бесконечно листать
и разбираться самому, что с чем работает. Отбор уже сделан за него.

Материал должен:
- показывать конкретное, применимое, продуманное и элегантное решение для дома;
- объяснять, почему оно работает;
- объяснять, с чем это сочетать;
- помогать выбирать меньше, но точнее.

Никогда не превращать текст в:
- Pinterest-подборку;
- generic luxury;
- интерьерный салон;
- свадебную эстетику;
- перегруженный декораторский текст.

Рубрики (использовать одну из подтверждённых, не придумывать новые):
- «Почему это работает»
- «С чем это сочетать»

Формат ответа — строго так, без вступлений от себя:

Заголовок: ...

Текст публикации: (готовый, законченный текст, который можно публиковать без правок)

Подписи/структура: (короткие подписи к фото или структура блоков)

Идея визуала: (что снять/показать — предметы, свет, ракурс, палитра: тёплый кремовый,
dusty pink, burgundy, olive, дерево, латунь; атмосферный солнечный свет и тени;
80% сдержанная editorial clarity + 20% чувственная историческая деталь)
"""

DEFAULT_TOPICS = [
    "Почему это работает — одна винтажная вещь в современном интерьере",
    "С чем это сочетать — латунь и оливковое стекло на одной полке",
]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "chat_id": None,
        "pending_draft": None,
        "last_topic": None,
        "schedule_hour": None,
        "schedule_minute": None,
        "topic_index": 0,
        "num_images": 3,
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()


def generate_draft(topic: str, feedback: str = None) -> str:
    if feedback:
        user_msg = (
            f"Тема: {topic}\n\n"
            f"Правки к предыдущему варианту: {feedback}\n\n"
            f"Перепиши черновик полностью с учётом правок, в том же формате."
        )
    else:
        user_msg = f"Подготовь готовую публикацию на тему: {topic}"

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "x-folder-id": YANDEX_FOLDER_ID,
        "Content-Type": "application/json",
    }
    body = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.6,
            "maxTokens": "2000",
        },
        "messages": [
            {"role": "system", "text": BRAND_PROMPT},
            {"role": "user", "text": user_msg},
        ],
    }

    resp = requests.post(YANDEX_TEXT_URL, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["alternatives"][0]["message"]["text"]


def extract_visual_idea(draft_text: str) -> str:
    marker = "Идея визуала:"
    idx = draft_text.find(marker)
    if idx == -1:
        # fallback — общий бренд-стиль, если модель не выдала блок в нужном формате
        return (
            "Интерьерная фотография в стиле editorial: тёплый кремовый, dusty pink, "
            "burgundy, olive, дерево, латунь, солнечный свет и мягкие тени"
        )
    idea = draft_text[idx + len(marker):].strip()
    return idea[:500]


def generate_image(prompt: str) -> bytes:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
    }
    body = {
        "modelUri": f"art://{YANDEX_FOLDER_ID}/yandex-art/latest",
        "generationOptions": {
            "seed": str(random.randint(0, 1_000_000)),
            "aspectRatio": {"widthRatio": "1", "heightRatio": "1"},
        },
        "messages": [{"weight": "1", "text": prompt}],
    }

    create_resp = requests.post(YANDEX_IMAGE_URL, headers=headers, json=body, timeout=30)
    create_resp.raise_for_status()
    op_id = create_resp.json()["id"]

    for _ in range(24):  # до ~2 минут ожидания
        time.sleep(5)
        op_resp = requests.get(YANDEX_OPERATION_URL.format(op_id=op_id), headers=headers, timeout=30)
        op_resp.raise_for_status()
        data = op_resp.json()
        if data.get("done"):
            if "error" in data:
                raise RuntimeError(data["error"])
            image_b64 = data["response"]["image"]
            return base64.b64decode(image_b64)

    raise TimeoutError("изображение не успело сгенерироваться за отведённое время")


async def send_draft(bot, chat_id: int, text: str, prefix: str = ""):
    await bot.send_message(chat_id=chat_id, text=f"{prefix}{text}\n\n—\nУтверждаем или правим?")
    idea = extract_visual_idea(text)
    count = state.get("num_images", 3)
    sent = 0
    for _ in range(count):
        try:
            image_bytes = generate_image(idea)
            await bot.send_photo(chat_id=chat_id, photo=io.BytesIO(image_bytes))
            sent += 1
        except Exception as e:
            logger.exception("Ошибка генерации изображения")
    if sent == 0:
        await bot.send_message(chat_id=chat_id, text="Текст готов, но ни одну картинку сгенерировать не получилось.")
    elif sent < count:
        await bot.send_message(chat_id=chat_id, text=f"Сгенерировала {sent} из {count} картинок — часть не получилась.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["chat_id"] = update.effective_chat.id
    save_state(state)
    await update.message.reply_text(
        "Готово, я на связи. Я — редактор POISE SELECT.\n\n"
        "Команды:\n"
        "/new тема — подготовить черновик публикации и картинки (без темы — возьму дежурную)\n"
        "/images N — сколько картинок присылать на черновик (сейчас: 3, максимум 5)\n"
        "/schedule ЧЧ:ММ — присылать черновик каждый день в это время (по времени сервера)\n"
        "/stop_schedule — выключить ежедневную рассылку\n\n"
        "После черновика просто напиши:\n"
        "«да» — утвердить и сохранить в архив\n"
        "«нет» — отклонить\n"
        "или текстом, что именно поправить"
    )


async def new_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args) if context.args else DEFAULT_TOPICS[state["topic_index"] % len(DEFAULT_TOPICS)]
    await update.message.reply_text("Готовлю черновик и картинку…")
    try:
        text = generate_draft(topic)
    except Exception as e:
        logger.exception("Ошибка генерации текста")
        await update.message.reply_text(f"Не получилось получить черновик: {e}")
        return
    state["pending_draft"] = text
    state["last_topic"] = topic
    save_state(state)
    await send_draft(context.bot, update.effective_chat.id, text)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    if not state.get("chat_id"):
        return
    topic = DEFAULT_TOPICS[state["topic_index"] % len(DEFAULT_TOPICS)]
    state["topic_index"] += 1
    try:
        text = generate_draft(topic)
    except Exception as e:
        logger.exception("Ошибка генерации по расписанию")
        await context.bot.send_message(chat_id=state["chat_id"], text=f"Не получилось подготовить дежурный черновик: {e}")
        return
    state["pending_draft"] = text
    state["last_topic"] = topic
    save_state(state)
    await send_draft(context.bot, state["chat_id"], text, prefix="Дежурный черновик на сегодня.\n\n")


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or ":" not in context.args[0]:
        await update.message.reply_text("Формат: /schedule 10:00")
        return
    hh, mm = context.args[0].split(":")
    hh, mm = int(hh), int(mm)

    for job in context.job_queue.get_jobs_by_name("daily_draft"):
        job.schedule_removal()

    context.job_queue.run_daily(scheduled_job, time=dtime(hour=hh, minute=mm), name="daily_draft")
    state["schedule_hour"], state["schedule_minute"] = hh, mm
    save_state(state)
    await update.message.reply_text(f"Готово. Черновик буду присылать каждый день в {hh:02d}:{mm:02d} (время сервера).")


async def images_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Формат: /images 3 (сейчас: {state.get('num_images', 3)})")
        return
    n = max(1, min(int(context.args[0]), 5))
    state["num_images"] = n
    save_state(state)
    await update.message.reply_text(f"Готово. Буду присылать {n} картинки(у) на каждый черновик.")


async def stop_schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for job in context.job_queue.get_jobs_by_name("daily_draft"):
        job.schedule_removal()
    state["schedule_hour"], state["schedule_minute"] = None, None
    save_state(state)
    await update.message.reply_text("Ежедневная рассылка выключена.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip().lower()

    if not state.get("pending_draft"):
        await update.message.reply_text("Нет черновика на утверждение. Начни с /new тема")
        return

    if msg in ("да", "утверждаю", "ок", "хорошо", "утвердить"):
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(state["pending_draft"] + "\n\n====\n\n")
        state["pending_draft"] = None
        save_state(state)
        await update.message.reply_text(
            "Принято. Материал сохранён в approved_drafts.log — можно публиковать в Дзен."
        )
    elif msg in ("нет", "отклонить", "отмена"):
        state["pending_draft"] = None
        save_state(state)
        await update.message.reply_text("Черновик отклонён.")
    else:
        await update.message.reply_text("Вношу правки и пересобираю картинку…")
        try:
            text = generate_draft(state["last_topic"], feedback=update.message.text)
        except Exception as e:
            logger.exception("Ошибка генерации правок")
            await update.message.reply_text(f"Не получилось внести правки: {e}")
            return
        state["pending_draft"] = text
        save_state(state)
        await send_draft(context.bot, update.effective_chat.id, text)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_draft))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("stop_schedule", stop_schedule_cmd))
    app.add_handler(CommandHandler("images", images_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if state.get("chat_id") and state.get("schedule_hour") is not None:
        app.job_queue.run_daily(
            scheduled_job,
            time=dtime(hour=state["schedule_hour"], minute=state["schedule_minute"]),
            name="daily_draft",
        )

    app.run_polling()


if __name__ == "__main__":
    main()
