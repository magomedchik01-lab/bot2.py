import os
from flask import Flask
from threading import Thread
import requests
import time
import json

TOKEN = "8396577657:AAF_ngr_-UmraTC4pISi1wdZS9BHsPuCAAE"  # вставьте свой токен
ADMIN_ID = 8002385540      # ваш Telegram ID
OWNER_ID = 8002385540      # ваш Telegram ID
CHANNEL_ID = -1001234567890  # chat_id вашего канала

API_URL = f"https://api.telegram.org/bot{TOKEN}/"

main_keyboard = {
    "keyboard": [
        [{"text": "📦 Сделать заказ"}, {"text": "💰 Прайс-лист"}],
        [{"text": "🧩 Наши работы"}, {"text": "⭐ Отзывы"}],
        [{"text": "ℹ️ О нас"}, {"text": "📞 Связаться"}]
    ],
    "resize_keyboard": True
}

service_keyboard = {
    "keyboard": [
        [{"text": "🤖 Telegram-бот"}, {"text": "🎬 Монтаж видео"}],
        [{"text": "🎨 Дизайн / Логотип"}, {"text": "🌐 Сайт"}],
        [{"text": "⚙️ Другое"}, {"text": "🔙 Назад"}]
    ],
    "resize_keyboard": True
}

user_states = {}
order_data = {}
pending_users = set()  # ждущие подписки

# ---------- Функции ----------
def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    requests.post(API_URL + "sendMessage", data=data)

def send_photo_url(chat_id, url, caption):
    data = {"chat_id": chat_id, "photo": url, "caption": caption}
    requests.post(API_URL + "sendPhoto", data=data)

def get_updates(offset=None):
    params = {"timeout": 10, "offset": offset}
    response = requests.get(API_URL + "getUpdates", params=params)
    if response.status_code == 200:
        return response.json().get("result", [])

def check_subscription(user_id):
    if user_id == OWNER_ID:
        return True
    try:
        resp = requests.get(API_URL + "getChatMember", params={"chat_id": CHANNEL_ID, "user_id": user_id})
        if resp.status_code == 200:
            status = resp.json()["result"]["status"]
            if status in ["member", "administrator", "creator"]:
                return True
    except:
        pass
    return False

def send_not_subscribed(chat_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "🎯 Проверить", "callback_data": "not_subscribed"}],
            [{"text": "🔗 Подписаться на канал", "url": "https://t.me/it_studio_channels"}]
        ]
    }
    
    # ---------- Автопроверка подписки ----------
    for user_id in list(pending_users):
        if check_subscription(user_id):
            send_message(user_id, "✅ Вы подписались! Добро пожаловать в IT Studio | Боты | Дизайн | Монтаж", main_keyboard)
            pending_users.remove(user_id)
            user_states[user_id] = None

    time.sleep(0.1)
    
    requests.post(API_URL + "sendMessage", data={
        "chat_id": chat_id,
        "text": "Вы должны подписаться на канал, чтобы продолжить:",
        "reply_markup": json.dumps(keyboard)
    })

def answer_callback(callback_id, text):
    requests.post(API_URL + "answerCallbackQuery", data={"callback_query_id": callback_id, "text": text, "show_alert": True})

# ---------- Основной цикл ----------
last_update_id = None

# Ссылки на фото (замени на свои, если нужно)
PHOTO_PRICE = "https://postimg.cc/hzz0G7Zq"
PHOTO_ABOUT = "https://postimg.cc/JtHTrSSK"
PHOTO_WORKS = "https://postimg.cc/MvvzpZqt"
PHOTO_REVIEWS = "https://postimg.cc/zV0H9G0Z"

while True:
    updates = get_updates(offset=last_update_id)
    for update in updates:
        last_update_id = update["update_id"] + 1

        # ---------- Callback inline кнопок ----------
        if "callback_query" in update:
            callback = update["callback_query"]
            callback_id = callback["id"]
            data = callback["data"]
            if data == "not_subscribed":
                answer_callback(callback_id, "🎯 Проверить")
            continue


        # ---------- Сообщения ----------
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")
            state = user_states.get(chat_id)

            # ---------- /start ----------
            if text == "/start":
                if not check_subscription(chat_id):
                    send_not_subscribed(chat_id)
                    pending_users.add(chat_id)
                else:
                    send_message(chat_id, "✅ Вы подписались! Добро пожаловать в IT Studio | Боты | Дизайн | Монтаж", main_keyboard)
                    if chat_id in pending_users:
                        pending_users.remove(chat_id)
                    user_states[chat_id] = None
                    # ---------- Пошаговый заказ ----------
            elif state == "waiting_service":
                if text == "🔙 Назад":
                    user_states[chat_id] = None
                    send_message(chat_id, "Главное меню 👇", main_keyboard)
                else:
                    order_data[chat_id] = {"service": text}
                    user_states[chat_id] = "waiting_description"
                    send_message(chat_id, "✍️ Опишите, что вам нужно:")

            elif state == "waiting_description":
                order_data[chat_id]["description"] = text
                user_states[chat_id] = "waiting_budget"
                send_message(chat_id, "💰 Укажите ваш бюджет:")

            elif state == "waiting_budget":
                order_data[chat_id]["budget"] = text
                order = order_data[chat_id]
                order_text = (
                    f"📦 *Новый заказ!*\n\n"
                    f"👤 Клиент: @{msg['from'].get('username', 'Не указан')}\n"
                    f"🛠 Услуга: {order['service']}\n"
                    f"📝 Описание: {order['description']}\n"
                    f"💰 Бюджет: {order['budget']}"
                )
                requests.post(API_URL + "sendMessage", data={"chat_id": ADMIN_ID, "text": order_text, "parse_mode": "Markdown"})
                send_message(chat_id, "✅ Заказ отправлен! Мы скоро с вами свяжемся 👌", main_keyboard)
                user_states[chat_id] = None
                order_data.pop(chat_id)

            # ---------- Главное меню ----------
            elif text == "📦 Сделать заказ":
                if not check_subscription(chat_id):
                    send_not_subscribed(chat_id)
                    pending_users.add(chat_id)
                else:
                    send_message(chat_id, "🛠 Выберите услугу:", service_keyboard)
                    user_states[chat_id] = "waiting_service"

            elif text == "💰 Прайс-лист":
                send_photo_url(chat_id, PHOTO_PRICE,
                               "💰 Прайс-лист:\n🤖 Боты — от 30$ / 2500₽\n🎬 Монтаж — от 10$ / 900₽\n🎨 Дизайн — от 15$ / 1300₽\n🌐 Сайт / автоматизация — от 50$ / 4500₽")

            elif text == "ℹ️ О нас":
                send_photo_url(chat_id, PHOTO_ABOUT,
                               "💻 IT Studio | Боты | Дизайн | Монтаж\nМы — современная IT-студия, которая создаёт цифровые решения под ключ.\nРаботаем строго по запросу клиента и подбираем решение именно под вашу задачу.\n🤖 Telegram-боты\n— автоматизация бизнеса\n— боты под заказы, магазины, сервисы\n— индивидуальная логика и дизайн\n🎬 Монтаж видео\n— YouTube / TikTok / Reels/— динамичный монтаж, эффекты, звук\n🎨 Дизайн и логотипы\n— логотипы, баннеры, обложки\n— фирменный стиль\n🌐 Сайты и автоматизация\n— простые сайты\n— интеграции и автоматизация процессов\nМы ценим качество, скорость и результат.\nРаботаем до полного одобрения клиента.")

            elif text == "🧩 Наши работы":
                send_photo_url(chat_id, PHOTO_WORKS, "🧩 Наши работы\nСкоро здесь будут реальные проекты")

            elif text == "⭐ Отзывы":
                send_photo_url(chat_id, PHOTO_REVIEWS, "⭐ Отзывы клиентов\nСсылка на чат с отзывами будет здесь")

            elif text == "📞 Связаться":
                send_message(chat_id, "📞 Связаться с нами\nГотовы обсудить ваш проект или ответить на любые вопросы.\n👤 Менеджер: @skillsboys\n💬 Консультация — бесплатно\n⚡ Быстрый ответ и индивидуальный подход\nНапишите нам, опишите задачу — мы предложим оптимальное решение под ваши цели и бюджет.")

    # ---------- Автопроверка подписки ----------
    for user_id in list(pending_users):
        if check_subscription(user_id):
            send_message(user_id, "✅ Вы подписались! Добро пожаловать в IT Studio | Боты | Дизайн | Монтаж", main_keyboard)
            pending_users.remove(user_id)
            user_states[user_id] = None


    time.sleep(0.1)
