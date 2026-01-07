import asyncio
import os
import logging
import json
from datetime import datetime
import io
from collections import Counter
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from aiogram.types import FSInputFile, LabeledPrice, PreCheckoutQuery
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramEntityTooLarge, TelegramConflictError
import yt_dlp
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Бот запущен!"

def run():
    app.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run)
    t.start()

# Попытка импорта openai для AI чата
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

# Попытка импорта matplotlib для графиков
try:
    import matplotlib
    matplotlib.use('Agg') # Исправление для работы на сервере без экрана
    import matplotlib.pyplot as plt
except Exception as e:
    print(f"⚠️ Ошибка импорта matplotlib: {e}")
    plt = None

# --- НАСТРОЙКИ ---
TOKEN = os.getenv('BOT_TOKEN')  # Вставь сюда токен
# OPENAI_API_KEY = "ВСТАВЬ_СЮДА_СВОЙ_КЛЮЧ_OPENAI" # Ключ от ChatGPT (начинается на sk-...)

# Адрес твоего локального сервера (если запущен). Если нет - закомментируй строки с session
# ВАЖНО: Чтобы работали файлы до 2000 МБ, нужно запустить telegram-bot-api отдельно.
# Команда для запуска (в командной строке):
# telegram-bot-api.exe --api-id=ТВОЙ_API_ID --api-hash=ТВОЙ_API_HASH --local
# LOCAL_SERVER_URL = "http://localhost:8081"

# Включаем HTML разметку по умолчанию (универсальный способ для разных версий aiogram)
try:
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
except ImportError:
    bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)

session = None

dp = Dispatcher()

# --- НАСТРОЙКИ ЗАРАБОТКА ---
ADMIN_ID = 8002385540  # Твой ID (взял из соседнего файла), чтобы выдавать премиум
# PAYMENT_TOKEN больше не нужен для Telegram Stars
CHANNEL_ID = "@it_studio_channels"  # Используем юзернейм канала (так надежнее для публичных)
CHANNEL_LINK = "https://t.me/it_studio_channels"
DAILY_LIMIT = 5        # Сколько видео можно скачать бесплатно в день
USERS_FILE = "users.json" # Файл, где храним данные пользователей
PROMO_FILE = "promocodes.json" # Файл с промокодами
AD_SETTINGS_FILE = "ad_settings.json" # Файл для хранения текущего спонсора

# --- СОСТОЯНИЯ (FSM) ---
class BotStates(StatesGroup):
    waiting_for_promo = State()
    waiting_for_support = State() # Ожидание сообщения в поддержку
    admin_replying = State()      # Админ пишет ответ
    ai_chat = State()             # Режим общения с AI
    ai_image = State()            # Режим генерации фото
    broadcasting = State()        # Рассылка (админ)
    ad_broadcasting = State()     # Рекламная рассылка (админ)
    waiting_for_ad_content = State() # Ожидание поста для авто-рекламы
    waiting_for_sponsor_text = State() # Ожидание текста спонсора
    waiting_for_sponsor_link = State() # Ожидание ссылки спонсора
    admin_waiting_promo = State() # Админ создает промокод
    admin_waiting_premium = State() # Админ выдает премиум

# --- БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ ---
if os.path.exists(USERS_FILE):
    with open(USERS_FILE, "r") as f:
        users_db = json.load(f)
else:
    users_db = {}

# --- БАЗА ДАННЫХ ПРОМОКОДОВ ---
if os.path.exists(PROMO_FILE):
    with open(PROMO_FILE, "r") as f:
        promo_db = json.load(f)
else:
    promo_db = {}

# --- НАСТРОЙКИ РЕКЛАМЫ (СПОНСОР) ---
if os.path.exists(AD_SETTINGS_FILE):
    with open(AD_SETTINGS_FILE, "r") as f:
        ad_settings = json.load(f)
else:
    ad_settings = {"sponsor_text": None, "sponsor_link": None, "expires_at": None, "user_id": None}

def save_db():
    with open(USERS_FILE, "w") as f:
        json.dump(users_db, f, indent=4)

def save_promos():
    with open(PROMO_FILE, "w") as f:
        json.dump(promo_db, f, indent=4)

def save_ad_settings():
    with open(AD_SETTINGS_FILE, "w") as f:
        json.dump(ad_settings, f, indent=4)

def check_limits(user_id):
    user_id = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if user_id not in users_db:
        users_db[user_id] = {
            "date": today, 
            "count": 0, 
            "premium": False, 
            "joined_at": today,
            "extra_limit": 0, # Дополнительные лимиты от промокодов
            "last_bonus": None, # Дата последнего бонуса
            "used_promos": []
        }
    
    user = users_db[user_id]
    
    # Если наступил новый день, сбрасываем счетчик
    if user["date"] != today:
        user["date"] = today
        user["count"] = 0
        save_db()
    
    # Миграция для старых пользователей (если добавили новые поля)
    if "last_bonus" not in user:
        user["last_bonus"] = None
    # Поле для хранения ID последнего сообщения бота (чтобы удалять его)
    if "last_msg_id" not in user:
        user["last_msg_id"] = None
    
    # Лимит = Стандартный (5) + Дополнительный (от промокодов)
    total_limit = DAILY_LIMIT + user.get("extra_limit", 0)
    
    # Разрешаем, если есть премиум ИЛИ лимит не исчерпан
    return user["premium"] or user["count"] < total_limit

# Включаем логирование, чтобы видеть ошибки в консоли
logging.basicConfig(level=logging.INFO)

# --- ФУНКЦИЯ УДАЛЕНИЯ СТАРОГО СООБЩЕНИЯ ---
async def delete_last_bot_msg(user_id, chat_id):
    user = users_db.get(user_id)
    if user and user.get("last_msg_id"):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=user["last_msg_id"])
        except Exception:
            pass # Если сообщение уже удалено или слишком старое
        user["last_msg_id"] = None

# --- ФУНКЦИЯ ПРОВЕРКИ ПОДПИСКИ ---
async def check_sub(user_id):
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        print(f"DEBUG: Пользователь {user_id}, статус в канале: {member.status}")
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        print(f"DEBUG: Ошибка проверки подписки: {e}")
        return False

def get_sub_keyboard():
    buttons = [
        [types.InlineKeyboardButton(text="🔗 Подписаться", url=CHANNEL_LINK)],
        [types.InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_menu(user_id=None):
    kb = [
        [types.KeyboardButton(text="👤 Профиль"), types.KeyboardButton(text="🎟 Промокод")],
        [types.KeyboardButton(text="💎 Премиум"), types.KeyboardButton(text="🆘 Поддержка")],
        [types.KeyboardButton(text="📢 Поделиться"), types.KeyboardButton(text="💼 Реклама")],
        [types.KeyboardButton(text="📖 Помощь")]
    ]
    if user_id == ADMIN_ID:
        kb.append([types.KeyboardButton(text="👑 Админ-панель")])
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- ФУНКЦИЯ СКАЧИВАНИЯ ---
def download_content(url, download_type="video", max_size_mb=50, tracker=None, quality=None):
    """
    Скачивает контент (видео или аудио).
    """
    max_bytes = max_size_mb * 1024 * 1024
    
    ydl_opts = {
        'cookiefile': 'cookies.txt',
        'outtmpl': 'downloads/%(id)s.%(ext)s',  # Куда сохранять (папка downloads)
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,  # Не скачивать плейлисты целиком
        'max_filesize': max_bytes,  # Лимит размера
        'concurrent_fragment_downloads': 5, # Ускорение: скачивание в 5 потоков (где возможно)
        'buffersize': 1024 * 1024, # Увеличенный буфер данных
        'progress_hooks': [],
        'paths': {'temp': 'downloads'}, # Храним временные файлы там же, где и основные (важно для серверов)
        'no_cache_dir': True, # Отключаем кэш, чтобы не занимать место
        'socket_timeout': 30, # Таймаут соединения
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

    # Если есть файл cookies.txt, используем его (помогает от блокировок Instagram/YouTube)
    if os.path.exists('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'

    # Хук для отслеживания прогресса
    def progress_hook(d):
        if d['status'] == 'downloading' and tracker is not None:
            p = d.get('_percent_str', '').strip()
            if p:
                tracker['percent'] = p
    
    if tracker is not None:
        ydl_opts['progress_hooks'] = [progress_hook]
    
    # Если нужна только обложка, просто получаем инфо
    if download_type == "thumbnail":
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get('thumbnail'), info.get('title', 'Video')
        except Exception as e:
            return None, None

    if download_type in ["audio", "voice"]:
        ydl_opts.update({
            'format': 'bestaudio/best', # Скачиваем аудио без конвертации (обычно m4a), чтобы работало без FFmpeg
        })
    elif download_type == "video":
        if quality:
            # Исправлено: более мягкий поиск формата, чтобы избежать ошибки "Requested format is not available"
            ydl_opts.update({'format': f'best[height<={quality}]/best'})
        else:
            ydl_opts.update({'format': 'best[ext=mp4]/best'})
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        # ИСПРАВЛЕНИЕ: Если файл не найден (например, yt-dlp сохранил как .mkv вместо .mp4), ищем его вручную
        if not os.path.exists(filename):
            file_id = info.get('id')
            if file_id and os.path.exists("downloads"):
                for f in os.listdir("downloads"):
                    # Ищем файл, который начинается с ID видео
                    if f.startswith(file_id):
                        filename = os.path.join("downloads", f)
                        break
        
    return filename, info.get('title', 'Video')

# --- ХЕНДЛЕРЫ (ОБРАБОТЧИКИ) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = str(message.from_user.id)
    check_limits(user_id) # Инициализируем пользователя в БД
    
    # Удаляем сообщение пользователя (/start) и старое меню бота
    try:
        await message.delete()
    except:
        pass
    await delete_last_bot_msg(user_id, message.chat.id)

    if not await check_sub(message.from_user.id):
        msg = await message.answer("❌ **Для использования бота подпишись на наш канал!**", reply_markup=get_sub_keyboard())
        users_db[user_id]["last_msg_id"] = msg.message_id
        return
    
    # --- РЕФЕРАЛЬНАЯ СИСТЕМА ---
    args = message.text.split()
    referrer_id = args[1] if len(args) > 1 else None

    # Если пользователя нет в базе (новичок) и есть реферрер
    if user_id not in users_db and referrer_id:
        if referrer_id in users_db and referrer_id != user_id:
            # Начисляем бонус пригласившему
            users_db[referrer_id]["extra_limit"] = users_db[referrer_id].get("extra_limit", 0) + 2
            save_db()
            try:
                await bot.send_message(referrer_id, "🎉 <b>Новый реферал!</b>\nКто-то запустил бота по твоей ссылке.\n➕ Тебе начислено +2 скачивания на сегодня!")
            except Exception:
                pass

    msg = await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я помогу тебе скачать видео или музыку из TikTok, Instagram и YouTube.\n"
        "Просто отправь мне ссылку!\n\n"
        "👇 Используй меню для управления:",
        reply_markup=get_main_menu(message.from_user.id)
    )
    users_db[user_id]["last_msg_id"] = msg.message_id  
    save_db()

@dp.message(F.text == "📖 Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    user_id = message.from_user.id
    
    help_text = (
        "📖 <b>Помощь и инструкции</b>\n\n"
        "<b>📥 Как скачать видео?</b>\n"
        "1. Скопируйте ссылку на видео (TikTok, Instagram, YouTube, Shorts, Reels).\n"
        "2. Отправьте ссылку боту в чат.\n"
        "3. Выберите нужный формат:\n"
        "   🎥 <b>Видео</b> — скачать видеофайл.\n"
        "   🎵 <b>Аудио</b> — извлечь звук (MP3).\n"
        "   🖼 <b>Обложка</b> — скачать картинку превью.\n\n"
        "⚠️ <b>Важная информация:</b>\n"
        "• Бот скачивает файлы до <b>500 МБ</b> (ограничение Telegram).\n"
        "• Если видео не скачивается, возможно, оно слишком длинное или профиль автора закрыт.\n"
        "• Бесплатный лимит: <b>5 скачиваний в день</b>.\n"
        "• <b>Premium</b> снимает лимиты по количеству.\n\n"
        "<b>🤖 Доступные команды:</b>\n"
        "/start — Перезапустить бота / Меню\n"
        "/premium — Купить вечный доступ\n"
        "/help — Показать это сообщение\n"
        "🆘 Поддержка — Написать админу\n"
    )

    if user_id == ADMIN_ID:
        help_text += (
            "\n<b>👑 Команды администратора:</b>\n"
            "/stats — Статистика пользователей\n"
            "/broadcast — Рассылка по всем юзерам\n"
            "/send_ad — Реклама (только для бесплатных)\n"
            "/add_promo CODE type value — Создать промокод\n"
            "/give_premium ID — Выдать премиум вручную\n"
        )
    
    await message.answer(help_text)

@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: types.CallbackQuery):
    if await check_sub(callback.from_user.id):
        await callback.message.delete()
        await callback.message.answer("✅ Спасибо! Ты подписан.\nПришли ссылку на видео!", reply_markup=get_main_menu(callback.from_user.id))
    else:
        await callback.answer("❌ Ты еще не подписался!", show_alert=True)

# --- МЕНЮ: ПРОФИЛЬ ---
@dp.message(F.text == "👤 Профиль")
async def menu_profile(message: types.Message):
    user_id = str(message.from_user.id)
    check_limits(user_id) # Обновляем/создаем запись
    user = users_db[user_id]
    
    status = "💎 PREMIUM" if user["premium"] else "👤 Обычный"
    total_limit = DAILY_LIMIT + user.get("extra_limit", 0)
    left = "♾ Безлимит" if user["premium"] else f"{total_limit - user['count']} из {total_limit}"
    
    text = (
        f"👤 <b>Твой профиль:</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📅 Дата регистрации: {user.get('joined_at', 'Неизвестно')}\n"
        f"📊 Статус: <b>{status}</b>\n"
        f"📉 Лимиты на сегодня: <b>{left}</b>"
    )
    
    # Кнопки профиля (Бонус и Донат)
    kb_inline = []
    today = datetime.now().strftime("%Y-%m-%d")
    
    if user.get("last_bonus") != today:
        kb_inline.append([types.InlineKeyboardButton(text="🎁 Забрать ежедневный бонус (+3)", callback_data="daily_bonus")])
    
    kb_inline.append([types.InlineKeyboardButton(text="💰 Поддержать автора (50 ⭐️)", callback_data="donate_author")])
    
    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_inline))

# --- МЕНЮ: ПРЕМИУМ ---
@dp.message(F.text == "💎 Премиум")
@dp.message(Command("premium"))
async def menu_premium(message: types.Message):
    # Тут напиши свои условия и контакты
    await message.answer("💎 <b>Премиум доступ навсегда</b>\n\n✅ Лимит до 2 ГБ (для больших видео)\n✅ Скачивание музыки (MP3)\n✅ Приоритетная поддержка\n✅ Никакой рекламы\n\n⭐️ <b>Цена: 150 Звезд (Telegram Stars)</b>")
    
    # Формируем счет
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Премиум доступ (Навсегда)",
        description="Снятие всех лимитов на скачивание видео.",
        payload="premium_forever",
        provider_token="", # Для Stars токен должен быть пустым
        currency="XTR", # Валюта Telegram Stars
        prices=[LabeledPrice(label="Премиум доступ", amount=150)], # 150 Звезд
        start_parameter="buy_premium"
    )

# --- ОБРАБОТКА ОПЛАТЫ ---
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    
    if payload == "premium_forever":
        # Выдаем премиум
        check_limits(user_id) # Убедимся, что юзер есть в БД
        users_db[user_id]["premium"] = True
        save_db()
        
        await message.answer(
            f"🎉 <b>Оплата прошла успешно!</b> (Сумма: {payment_info.total_amount} {payment_info.currency})\n\n"
            "💎 Теперь у тебя <b>PREMIUM</b> статус навсегда!\n"
            "Лимиты сняты. Приятного пользования!"
        )
        
        # Уведомляем админа
        await bot.send_message(ADMIN_ID, f"💰 <b>НОВАЯ ПРОДАЖА!</b>\nПользователь {message.from_user.full_name} (ID: {user_id}) купил премиум за 150 Stars.")

    elif payload == "ad_broadcast_payment":
        data = await state.get_data()
        ad_msg_id = data.get("ad_message_id")
        ad_chat_id = data.get("ad_chat_id")
        
        if not ad_msg_id:
            await message.answer("❌ Ошибка: пост не найден. Свяжитесь с админом.")
            return

        await message.answer("✅ <b>Оплата прошла успешно!</b>\n🚀 Запускаю автоматическую рассылку...")
        
        # Автоматическая рассылка
        non_premium_count = 0
        sent_count = 0
        
        for uid, u_data in users_db.items():
            if not u_data.get("premium", False):
                non_premium_count += 1
                try:
                    # Копируем рекламный пост пользователю
                    await bot.copy_message(chat_id=uid, from_chat_id=ad_chat_id, message_id=ad_msg_id)
                    sent_count += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
        
        await message.answer(f"✅ <b>Рассылка завершена!</b>\nДоставлено: {sent_count} из {non_premium_count} пользователей.")
        await bot.send_message(ADMIN_ID, f"💰 <b>ПРОДАНА РЕКЛАМА!</b>\nЮзер {user_id} купил рассылку за 750 Stars.\nОхват: {sent_count} чел.")
        await state.clear()

    elif payload == "ad_sponsor_payment":
        data = await state.get_data()
        s_text = data.get("sponsor_text")
        s_link = data.get("sponsor_link")
        
        # Устанавливаем спонсора на 24 часа
        expires = datetime.now().timestamp() + 24 * 3600
        ad_settings["sponsor_text"] = s_text
        ad_settings["sponsor_link"] = s_link
        ad_settings["expires_at"] = expires
        ad_settings["user_id"] = int(user_id) # Запоминаем ID спонсора
        save_ad_settings()
        
        await message.answer("✅ <b>Оплата прошла успешно!</b>\n💎 Вы стали спонсором скачиваний на 24 часа.\nВаша ссылка будет добавлена ко всем видео.")
        await bot.send_message(ADMIN_ID, f"💰 <b>НОВЫЙ СПОНСОР!</b>\nЮзер {user_id} купил спонсорку за 250 Stars.\nТекст: {s_text}")
        await state.clear()

    elif payload == "limit_pack_20":
        check_limits(user_id)
        users_db[user_id]["extra_limit"] = users_db[user_id].get("extra_limit", 0) + 20
        save_db()
        
        await message.answer(f"🎉 <b>Оплата успешна!</b>\n➕ Добавлено 20 скачиваний к твоему лимиту.")
        await bot.send_message(ADMIN_ID, f"💰 <b>ПРОДАЖА ЛИМИТОВ!</b>\nЮзер {user_id} купил 20 скачиваний за 25 Stars.")

    elif payload == "donation_50":
        await message.answer("🙏 <b>Спасибо за поддержку!</b>\nБлагодаря вам бот становится лучше.")
        await bot.send_message(ADMIN_ID, f"💰 <b>ДОНАТ!</b>\nЮзер {user_id} пожертвовал 50 Stars.")

# --- МЕНЮ: ПОДДЕРЖКА ---
@dp.message(F.text == "🆘 Поддержка")
async def menu_support(message: types.Message, state: FSMContext):
    await message.answer("👨‍💻 <b>Служба поддержки</b>\n\nОпишите вашу проблему или вопрос одним сообщением, и мы ответим вам в ближайшее время 👇")
    await state.set_state(BotStates.waiting_for_support)

@dp.message(BotStates.waiting_for_support)
async def process_support_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text
    
    # Клавиатура для админа
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="👀 На рассмотрение", callback_data=f"sup_review:{user_id}")],
        [types.InlineKeyboardButton(text="✍️ Ответить", callback_data=f"sup_reply:{user_id}")],
        [types.InlineKeyboardButton(text="✅ Решено", callback_data=f"sup_solve:{user_id}")]
    ])
    
    await bot.send_message(
        ADMIN_ID,
        f"📩 <b>НОВЫЙ ТИКЕТ</b>\n\n👤 От: {message.from_user.full_name} (ID: <code>{user_id}</code>)\n📄 Текст: {text}\n\nСтатус: 🆕 Новый",
        reply_markup=kb
    )
    
    await message.answer("✅ Ваше сообщение отправлено! Ожидайте ответа.")
    await state.clear()

# --- АДМИН: ОБРАБОТКА ТИКЕТОВ ---
@dp.callback_query(F.data.startswith("sup_"))
async def admin_support_actions(callback: types.CallbackQuery, state: FSMContext):
    action, user_id = callback.data.split(":")
    
    if action == "sup_review":
        # Статус: На рассмотрении
        new_text = callback.message.text.replace("Статус: 🆕 Новый", "Статус: 👀 На рассмотрении")
        if new_text == callback.message.text: # Если уже был изменен
             new_text = callback.message.text + "\n(Взято в работу)"
             
        await callback.message.edit_text(new_text, reply_markup=callback.message.reply_markup)
        await bot.send_message(user_id, "👨‍💻 <b>Поддержка:</b> Ваш запрос взят на рассмотрение.")
        await callback.answer("Статус изменен: На рассмотрении")
        
    elif action == "sup_solve":
        # Статус: Решено
        await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>РЕШЕНО</b>", reply_markup=None)
        await bot.send_message(user_id, "✅ <b>Поддержка:</b> Ваша проблема решена! Если что-то еще — пишите.")
        await callback.answer("Тикет закрыт")
        
    elif action == "sup_reply":
        # Админ хочет ответить
        await callback.message.answer(f"✍️ Введите ответ для пользователя <code>{user_id}</code>:")
        await state.update_data(reply_user_id=user_id)
        await state.set_state(BotStates.admin_replying)
        await callback.answer()

@dp.message(BotStates.admin_replying)
async def admin_send_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("reply_user_id")
    
    if target_id:
        try:
            await bot.send_message(target_id, f"👨‍💻 <b>Ответ поддержки:</b>\n\n{message.text}")
            await message.answer("✅ Ответ отправлен.")
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить (возможно, юзер заблокировал бота): {e}")
    
    await state.clear()

# --- МЕНЮ: ПОДЕЛИТЬСЯ ---
@dp.message(F.text == "📢 Поделиться")
async def menu_share(message: types.Message):
    bot_info = await bot.get_me()
    # Добавляем ID пользователя в ссылку для реферальной системы
    bot_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    share_url = f"https://t.me/share/url?url={bot_link}&text=Привет! Я скачиваю видео через этого бота. Попробуй тоже!"
    
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="↗️ Отправить другу", url=share_url)]
    ])
    await message.answer("👇 <b>Пригласи друга и получи бонусы!</b>\nЗа каждого друга: +2 скачивания.\n\nНажми кнопку ниже, чтобы отправить ссылку:", reply_markup=kb)

# --- МЕНЮ: РЕКЛАМА ---
@dp.message(F.text == "💼 Реклама")
async def menu_ads(message: types.Message):
    text = (
        "📈 <b>Реклама в боте</b>\n\n"
        "У нас активная аудитория, и вы можете заказать рассылку вашего объявления.\n\n"
        " <b>Выберите тариф:</b>\n\n"
        "1️⃣ <b>Быстрая рассылка</b> — <b>125 ⭐️</b>\n"
        "• Моментальная отправка вашего поста всем пользователям в ЛС.\n\n"
        "2️⃣ <b>Спонсор скачиваний (24ч)</b> — <b>250 ⭐️</b>\n"
        "• Ваша ссылка и текст добавляются в описание к КАЖДОМУ скачанному видео.\n"
        "• Максимальный охват и виральность."
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📢 Рассылка (125 ⭐️)", callback_data="buy_ad_broadcast")],
        [types.InlineKeyboardButton(text="💎 Спонсор (250 ⭐️)", callback_data="buy_ad_sponsor")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "buy_ad_broadcast")
async def callback_buy_ad(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("📢 <b>Покупка рекламы</b>\n\nПришлите сообщение (текст, фото или видео), которое нужно разослать.\nОно будет отправлено всем пользователям (кроме Premium) сразу после оплаты.")
    await state.set_state(BotStates.waiting_for_ad_content)
    await callback.answer()

@dp.message(BotStates.waiting_for_ad_content)
async def process_ad_content_input(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("👇 <b>Главное меню:</b>", reply_markup=get_main_menu(message.from_user.id))
        return

    # Сохраняем ID сообщения и чата, чтобы потом переслать
    await state.update_data(ad_message_id=message.message_id, ad_chat_id=message.chat.id)
    
    await message.answer("✅ Пост принят!\n\n💳 <b>К оплате: 125 Stars</b>\nПосле оплаты рассылка начнется автоматически.")
    
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Рекламная рассылка",
        description="Рассылка вашего поста по базе пользователей (кроме Premium).",
        payload="ad_broadcast_payment",
        provider_token="", # Stars
        currency="XTR",
        prices=[LabeledPrice(label="Рассылка", amount=125)],
        start_parameter="buy_ad"
    )

# --- ЛОГИКА ПОКУПКИ СПОНСОРА ---
@dp.callback_query(F.data == "buy_ad_sponsor")
async def callback_buy_sponsor(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("💎 <b>Спонсор скачиваний</b>\n\nВведите короткий текст кнопки (например: 'Подпишись на канал').\nМаксимум 30 символов.")
    await state.set_state(BotStates.waiting_for_sponsor_text)
    await callback.answer()

@dp.message(BotStates.waiting_for_sponsor_text)
async def process_sponsor_text(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("👇 <b>Главное меню:</b>", reply_markup=get_main_menu(message.from_user.id))
        return

    if len(message.text) > 30:
        await message.answer("❌ Слишком длинный текст. Попробуйте короче (до 30 символов).")
        return
    await state.update_data(sponsor_text=message.text)
    await message.answer("🔗 Теперь отправьте ссылку (на канал, сайт или бота):")
    await state.set_state(BotStates.waiting_for_sponsor_link)

@dp.message(BotStates.waiting_for_sponsor_link)
async def process_sponsor_link(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("👇 <b>Главное меню:</b>", reply_markup=get_main_menu(message.from_user.id))
        return

    link = message.text.strip()
    if not link.startswith(("http", "t.me")):
        await message.answer("❌ Это не похоже на ссылку. Ссылка должна начинаться с http или t.me")
        return
        
    await state.update_data(sponsor_link=link)
    
    await message.answer("✅ Данные приняты!\n\n💳 <b>К оплате: 250 Stars</b>\nВаша реклама будет размещена на 24 часа.")
    
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Спонсор скачиваний (24ч)",
        description="Размещение ссылки в описании всех скачиваемых видео.",
        payload="ad_sponsor_payment",
        provider_token="", # Stars
        currency="XTR",
        prices=[LabeledPrice(label="Спонсорство", amount=250)],
        start_parameter="buy_sponsor"
    )

# --- МЕНЮ: ПРОМОКОД ---
@dp.message(F.text == "🎟 Промокод")
async def menu_promo(message: types.Message, state: FSMContext):
    await message.answer("✍️ Введи промокод, чтобы получить бонусы:")
    await state.set_state(BotStates.waiting_for_promo)

@dp.message(BotStates.waiting_for_promo)
async def process_promo(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("👇 <b>Главное меню:</b>", reply_markup=get_main_menu(message.from_user.id))
        return

    code = message.text.strip()
    user_id = str(message.from_user.id)
    
    if code in promo_db:
        user = users_db.get(user_id)
        if not user:
            check_limits(user_id)
            user = users_db[user_id]
            
        # Проверка: использовал ли уже
        if "used_promos" not in user:
            user["used_promos"] = []
            
        if code in user["used_promos"]:
            await message.answer("❌ Ты уже активировал этот промокод!")
        else:
            # Активация
            promo = promo_db[code]
            if promo["type"] == "limit":
                user["extra_limit"] = user.get("extra_limit", 0) + promo["value"]
                await message.answer(f"✅ Промокод активирован!\n➕ Добавлено {promo['value']} скачиваний к лимиту.")
            elif promo["type"] == "premium":
                user["premium"] = True
                await message.answer("✅ Промокод активирован!\n💎 Поздравляем! Ты получил PREMIUM статус.")
            
            user["used_promos"].append(code)
            save_db()
    else:
        await message.answer("❌ Неверный промокод.")
    
    await state.clear()

# --- АДМИН: СОЗДАНИЕ ПРОМОКОДА ---
@dp.message(F.text == "🎟 Создать промо")
@dp.message(Command("add_promo"))
async def cmd_add_promo(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    # Формат: /add_promo CODE TYPE VALUE
    # Пример: /add_promo NEWYEAR limit 10
    try:
        args = message.text.split()
        code = args[1]
        p_type = args[2] # limit или premium
        value = int(args[3])
        
        promo_db[code] = {"type": p_type, "value": value}
        save_promos()
        await message.answer(f"✅ Промокод <code>{code}</code> создан! ({p_type}: {value})")
    except Exception:
        await message.answer("Ошибка. Используй: `/add_promo КОД limit 10` или `/add_promo КОД premium 1`")

@dp.message(F.text == "💎 Выдать Премиум")
@dp.message(Command("give_premium"))
async def cmd_give_premium(message: types.Message):
    # Команда только для админа: /give_premium 123456789
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        target_id = message.text.split()[1]
        if target_id in users_db:
            users_db[target_id]["premium"] = True
            save_db()
            await message.answer(f"✅ Премиум выдан пользователю {target_id}")
        else:
            await message.answer("⚠️ Пользователь не найден в базе (пусть сначала нажмет /start)")
    except IndexError:
        await message.answer("Используй: /give_premium ID_ПОЛЬЗОВАТЕЛЯ")

# --- АДМИН: РАССЫЛКА ---
@dp.message(F.text == "📢 Рассылка")
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("✍️ <b>Рассылка</b>\nПришли сообщение (текст, фото или видео).\n\n💡 <b>Совет:</b> Чтобы добавить кнопку-ссылку, используй формат:\n<code>Текст поста | Текст кнопки | https://ссылка</code>")
    await state.set_state(BotStates.broadcasting)

@dp.message(BotStates.broadcasting)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("👇 <b>Главное меню:</b>", reply_markup=get_main_menu(message.from_user.id))
        return

    users_count = len(users_db)
    await message.answer(f"🚀 Начинаю рассылку на {users_count} пользователей...")
    
    # Пробуем распарсить кнопку (если это текст)
    reply_markup = None
    text_to_send = message.text
    
    if message.text and "|" in message.text:
        parts = message.text.split("|")
        if len(parts) >= 3:
            text_to_send = parts[0].strip()
            btn_text = parts[1].strip()
            btn_url = parts[2].strip()
            if btn_url.startswith("http"):
                reply_markup = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text=btn_text, url=btn_url)]])

    count = 0
    for user_id in users_db:
        try:
            if reply_markup and message.text:
                await bot.send_message(chat_id=user_id, text=text_to_send, reply_markup=reply_markup)
            else:
                await message.copy_to(chat_id=user_id)
            count += 1
            await asyncio.sleep(0.05) # Задержка, чтобы не забанили за спам
        except Exception:
            pass
    await message.answer(f"✅ Рассылка завершена! Доставлено: {count}/{users_count}")
    await state.clear()

# --- АДМИН: РЕКЛАМНАЯ РАССЫЛКА (ТОЛЬКО НЕ ПРЕМИУМ) ---
@dp.message(F.text == "💼 Реклама (Free)")
@dp.message(Command("send_ad"))
async def cmd_send_ad(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("📢 <b>Рекламная рассылка</b>\nПришли сообщение (пост), которое увидят все пользователи <b>БЕЗ премиума</b>:")
    await state.set_state(BotStates.ad_broadcasting)

@dp.message(BotStates.ad_broadcasting)
async def process_ad_broadcast(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.clear()
        await message.answer("👇 <b>Главное меню:</b>", reply_markup=get_main_menu(message.from_user.id))
        return

    users_count = len(users_db)
    non_premium_count = 0
    
    # Считаем получателей (только без премиума)
    for uid, data in users_db.items():
        if not data.get("premium", False):
            non_premium_count += 1
    
    await message.answer(f"🚀 Начинаю отправку рекламы для {non_premium_count} пользователей (из {users_count})...")
    
    sent_count = 0
    for user_id, user_data in users_db.items():
        # Пропускаем премиум пользователей
        if user_data.get("premium", False):
            continue
            
        try:
            await message.copy_to(chat_id=user_id)
            sent_count += 1
            await asyncio.sleep(0.05) 
        except Exception:
            pass
            
    await message.answer(f"✅ Реклама отправлена! Доставлено: {sent_count}/{non_premium_count}")
    await state.clear()

# --- АДМИН: СТАТИСТИКА ---
@dp.message(F.text == "📊 Статистика")
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    if not plt:
        await message.answer("❌ Ошибка: библиотека matplotlib не загружена. Смотри ошибку в консоли.")
        return

    # Сбор данных
    total_users = len(users_db)
    premium_users = sum(1 for u in users_db.values() if u.get("premium"))
    
    # Данные для графика (по дате регистрации)
    dates = [u.get("joined_at", "Unknown") for u in users_db.values()]
    # Фильтруем некорректные даты
    dates = [d for d in dates if d != "Unknown"]
    
    if not dates:
        await message.answer(f"📊 <b>Статистика:</b>\n👥 Всего: {total_users}\n💎 Premium: {premium_users}\n(Мало данных для графика)")
        return

    date_counts = Counter(dates)
    sorted_dates = sorted(date_counts.keys())
    counts = [date_counts[d] for d in sorted_dates]

    # Построение графика
    try:
        plt.figure(figsize=(10, 6))
        plt.plot(sorted_dates, counts, marker='o', linestyle='-', color='b')
        plt.title("Рост аудитории (новые пользователи)")
        plt.xlabel("Дата")
        plt.ylabel("Кол-во")
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.tight_layout()

        # Сохранение в буфер
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()

        # Отправка
        photo = types.BufferedInputFile(buf.read(), filename="stats.png")
        await message.answer_photo(photo, caption=f"📊 <b>Статистика бота:</b>\n\n👥 Всего пользователей: {total_users}\n💎 Premium пользователей: {premium_users}")
    except Exception as e:
        await message.answer(f"❌ Ошибка построения графика: {e}")

# --- АДМИН ПАНЕЛЬ (МЕНЮ) ---
@dp.message(F.text == "👑 Админ-панель")
async def menu_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    kb = [
        [types.KeyboardButton(text="📢 Рассылка"), types.KeyboardButton(text="💼 Реклама (Free)")],
        [types.KeyboardButton(text="🎟 Создать промо"), types.KeyboardButton(text="💎 Выдать Премиум")],
        [types.KeyboardButton(text="📊 Статистика")],
        [types.KeyboardButton(text="🔙 Назад")]
    ]
    await message.answer("👑 <b>Админ-панель</b>\nВыберите действие:", reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True))

# --- AI ФУНКЦИИ (ЧАТ И ФОТО) ---
@dp.message(F.text == "🤖 AI Чат")
async def menu_ai_chat(message: types.Message, state: FSMContext):
    await message.answer("🤖 <b>Режим AI Чата</b>\n\nЗадай мне любой вопрос, и я постараюсь ответить!\n(Напиши 'Отмена' чтобы выйти)")
    await state.set_state(BotStates.ai_chat)

@dp.message(BotStates.ai_chat)
async def process_ai_chat(message: types.Message, state: FSMContext):
    if message.text.lower() == "отмена":
        await message.answer("Вышли из режима AI.", reply_markup=get_main_menu(message.from_user.id))
        await state.clear()
        return

    # Telegram API не умеет "думать", для этого нужен OpenAI API
    # Сюда нужно добавить код запроса к ChatGPT, если у тебя появится ключ
    # if not AsyncOpenAI or OPENAI_API_KEY.startswith("ВСТАВЬ"):
    #     await message.answer("⚠️ <b>AI функции не настроены.</b>\nВладельцу нужно установить библиотеку openai и вставить API ключ в код.")
    #     return

#     processing_msg = await message.answer("🤔 <b>Думаю...</b>")
    
#     await message.answer("⚠️ <b>AI функции пока не настроены.</b>\n\nСам Telegram API не умеет отвечать на вопросы — он только доставляет сообщения.\n\nЧтобы бот стал умным, владельцу нужно подключить <b>OpenAI API Key</b> (ChatGPT).")

#     try:
#         client = AsyncOpenAI(api_key=OPENAI_API_KEY)
#         response = await client.chat.completions.create(
#             model="gpt-3.5-turbo", # Можно поменять на gpt-4o, если есть доступ
#             messages=[{"role": "user", "content": message.text}]
#         )
#         answer_text = response.choices[0].message.content
#         await processing_msg.edit_text(answer_text) # Отправляем ответ
#     except Exception as e:
#         await processing_msg.edit_text(f"❌ <b>Ошибка AI:</b> {e}")
        
# @dp.message(F.text == "🎨 Создать фото")
# async def menu_ai_image(message: types.Message, state: FSMContext):
#     await message.answer("⚠️ Функция временно недоступна.")

# @dp.message(BotStates.ai_image)
# async def process_ai_image(message: types.Message, state: FSMContext):
#     await message.answer("⚠️ Функция временно недоступна.")

# --- НОВЫЕ ФУНКЦИИ (БОНУС И ДОНАТ) ---
@dp.callback_query(F.data == "daily_bonus")
async def callback_daily_bonus(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    check_limits(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if users_db[user_id].get("last_bonus") == today:
        await callback.answer("❌ Вы уже получали бонус сегодня!", show_alert=True)
        return
        
    users_db[user_id]["extra_limit"] = users_db[user_id].get("extra_limit", 0) + 3
    users_db[user_id]["last_bonus"] = today
    save_db()
    
    await callback.message.edit_text(f"🎁 <b>Бонус получен!</b>\n➕ Вам начислено +3 бесплатных скачивания на сегодня.\n\nЗаходите завтра за новой порцией!", reply_markup=None)

@dp.callback_query(F.data == "donate_author")
async def callback_donate_author(callback: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="Поддержка автора",
        description="Добровольное пожертвование на развитие бота.",
        payload="donation_50",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Чаевые", amount=50)],
        start_parameter="donate"
    )
    await callback.answer()

# --- ОБРАБОТКА ССЫЛОК ---
@dp.message(F.text.startswith(("http", "www")))
async def handle_link(message: types.Message, state: FSMContext):
    url = message.text.strip()
    user_id = str(message.from_user.id)
    
    # Удаляем сообщение с ссылкой (для чистоты чата)
    try:
        await message.delete()
    except:
        pass
    await delete_last_bot_msg(user_id, message.chat.id)
    
    # 0. Проверяем подписку на канал
    if not await check_sub(message.from_user.id):
        await message.answer("❌ <b>Для скачивания видео подпишись на канал!</b>", reply_markup=get_sub_keyboard())
        return
    
    # 1. Проверяем лимиты
    if not check_limits(user_id):
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Купить 20 скачиваний (25 ⭐️)", callback_data="buy_limit_pack")]
        ])
        await message.answer("❌ <b>Лимит на сегодня исчерпан!</b>\n\nТы скачал максимум бесплатных видео.\nПриходи завтра, купи безлимит (/premium) или докупи скачивания разово.", reply_markup=kb)
        return

    # Сохраняем ссылку и предлагаем выбор
    await state.update_data(url=url)
    
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🎥 Видео", callback_data="dl_video"),
            types.InlineKeyboardButton(text="🎵 Аудио", callback_data="dl_audio")
        ],
        [
            types.InlineKeyboardButton(text="🗣 Голосовое", callback_data="dl_voice"),
            types.InlineKeyboardButton(text="🖼 Обложка", callback_data="dl_thumb")
        ]
    ])
    msg = await message.answer("🎞 <b>Выберите формат загрузки:</b>", reply_markup=kb)
    users_db[user_id]["last_msg_id"] = msg.message_id
    save_db()

@dp.callback_query(F.data == "buy_limit_pack")
async def buy_limit_pack(callback: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="Пакет скачиваний (+20)",
        description="Дополнительные 20 скачиваний к вашему лимиту.",
        payload="limit_pack_20",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="20 скачиваний", amount=25)],
        start_parameter="buy_limits"
    )
    await callback.answer()

# --- ВЫБОР КАЧЕСТВА ВИДЕО ---
@dp.callback_query(F.data == "dl_video")
async def ask_video_quality(callback: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📱 360p", callback_data="qual_360"),
         types.InlineKeyboardButton(text="🖥 720p", callback_data="qual_720")],
        [types.InlineKeyboardButton(text="📺 1080p", callback_data="qual_1080"),
         types.InlineKeyboardButton(text="💎 Max", callback_data="qual_max")],
        [types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_format")]
    ])
    await callback.message.edit_text("🎞 <b>Выберите качество видео:</b>", reply_markup=kb)

@dp.callback_query(F.data == "back_to_format")
async def back_to_format(callback: types.CallbackQuery, state: FSMContext):
    # Возвращаем меню выбора формата
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🎥 Видео", callback_data="dl_video"),
            types.InlineKeyboardButton(text="🎵 Аудио", callback_data="dl_audio")
        ],
        [
            types.InlineKeyboardButton(text="🗣 Голосовое", callback_data="dl_voice"),
            types.InlineKeyboardButton(text="� Обложка", callback_data="dl_thumb")
        ]
    ])
    await callback.message.edit_text("🎞 <b>Выберите формат загрузки:</b>", reply_markup=kb)

# --- ОБЩАЯ ФУНКЦИЯ ЗАПУСКА СКАЧИВАНИЯ ---
async def start_download_process(callback: types.CallbackQuery, state: FSMContext, download_type, quality=None):
    data = await state.get_data()
    url = data.get("url")
    
    if not url:
        await callback.answer("Ссылка устарела, отправь заново.", show_alert=True)
        return

    user_id = str(callback.from_user.id)
    
    # Определяем лимит размера
    user = users_db.get(user_id, {})
    is_premium = user.get("premium", False)
    max_size = 50  # Стандартный лимит Telegram API (без своего сервера)

    await callback.message.edit_text(f"⏳ Начинаю загрузку...")
    
    # Трекер прогресса
    tracker = {'percent': '0%'}
    
    # Фоновая задача для обновления процентов в сообщении
    async def progress_monitor():
        last_msg = ""
        while True:
            await asyncio.sleep(2) # Обновляем не чаще чем раз в 2 сек
            pct = tracker['percent']
            msg = f"⏳ Скачиваю {download_type}... {pct}"
            if msg != last_msg and pct != '0%':
                try:
                    await callback.message.edit_text(msg)
                    last_msg = msg
                except:
                    pass
    
    # Запускаем скачивание (в отдельном потоке, чтобы бот не завис)
    monitor_task = asyncio.create_task(progress_monitor())
    loop = asyncio.get_event_loop()
    
    try:
        file_path, title = await loop.run_in_executor(None, download_content, url, download_type, max_size, tracker, quality)
    except Exception as e:
        err_msg = str(e)
        # Улучшенная обработка ошибки Instagram
        if "Instagram" in err_msg and "no video" in err_msg:
            await callback.message.edit_text("❌ <b>Ошибка Instagram:</b>\nБот не нашел видео в этом посте.\nВозможно, это просто фото, или Instagram заблокировал IP сервера.\n\n💡 <i>Решение: попробуйте добавить cookies.txt в корень бота.</i>")
        else:
            await callback.message.edit_text(f"❌ <b>Ошибка при скачивании:</b>\n\n<code>{err_msg}</code>\n\nПопробуйте другую ссылку или качество пониже.")
        return
    finally:
        monitor_task.cancel() # Останавливаем обновление процентов

    # Если это обложка, file_path будет URL картинки
    if download_type == "thumbnail":
        if file_path:
            await callback.message.answer_photo(file_path, caption=f"🖼 <b>Обложка видео:</b>\n{title}")
            await callback.message.delete()
        else:
            await callback.message.edit_text("❌ Не удалось найти обложку.")
        return

    if file_path and os.path.exists(file_path):
        try:
            await callback.message.edit_text("📤 Отправляю файл...")
            
            media_file = FSInputFile(file_path)
            
            # Формируем описание (Caption)
            caption = f"🎥 <b>{title}</b>\n🤖 Файл был скачайн с помощью бота @it_studio_videoBOT"
            
            # --- ДОБАВЛЕНИЕ РЕКЛАМЫ СПОНСОРА ---
            if ad_settings.get("expires_at") and datetime.now().timestamp() < ad_settings["expires_at"]:
                s_text = ad_settings.get("sponsor_text", "Реклама")
                s_link = ad_settings.get("sponsor_link", "")
                if s_text and s_link:
                    caption += f"\n\n💎 <b>Спонсор:</b> <a href='{s_link}'>{s_text}</a>"
            # -----------------------------------

            if download_type == "audio":
                await callback.message.answer_audio(media_file, caption=caption, title=title)
            elif download_type == "voice":
                # Пытаемся отправить как голосовое. Если формат не подходит, Telegram может ругаться, тогда шлем как аудио
                try:
                    await callback.message.answer_voice(media_file, caption=caption)
                except Exception:
                    await callback.message.answer_audio(media_file, caption=caption, title=title)
            else:
                await callback.message.answer_video(media_file, caption=caption)
            
            # 2. Если успешно отправили, списываем лимит
            if user_id in users_db:
                users_db[user_id]["count"] += 1
                save_db()
            
            await callback.message.delete()
            
            # Кнопка "Скачать еще"
            await callback.message.answer(
                "✅ <b>Загрузка завершена!</b>",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📥 Скачать еще", callback_data="download_more")]
                ])
            )
            
        except TelegramEntityTooLarge:
            await callback.message.edit_text("❌ <b>Файл слишком большой!</b>\n\nТелеграм не позволяет ботам отправлять файлы больше 50 МБ.\n💡 Попробуй скачать как <b>Аудио</b> — оно весит меньше.")
        except Exception as e:
            await callback.message.edit_text(f"❌ Ошибка при отправке: {e}")
        finally:
            # ОБЯЗАТЕЛЬНО удаляем файл с сервера после отправки
            if os.path.exists(file_path):
                os.remove(file_path)
    else:
        print(f"DEBUG: Файл не найден по пути: {file_path}") # Для отладки в консоли
        limit_msg = "50 МБ"
        await callback.message.edit_text(f"❌ Не удалось скачать.\n\nВозможно:\n1. Файл больше лимита ({limit_msg})\n2. Ссылка некорректна\n3. Закрытый профиль")

# --- ХЕНДЛЕРЫ ДЛЯ ЗАПУСКА ---
@dp.callback_query(F.data.in_({"dl_audio", "dl_thumb", "dl_voice"}))
async def process_simple_download(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "dl_audio":
        download_type = "audio"
    elif callback.data == "dl_voice":
        download_type = "voice"
    else:
        download_type = "thumbnail"
    await start_download_process(callback, state, download_type)

@dp.callback_query(F.data.startswith("qual_"))
async def process_quality_download(callback: types.CallbackQuery, state: FSMContext):
    quality = callback.data.split("_")[1]
    if quality == "max":
        quality = None # Максимальное качество (как раньше)
    
    await start_download_process(callback, state, "video", quality)

# --- ОБРАБОТКА КНОПКИ "СКАЧАТЬ ЕЩЕ" ---
@dp.callback_query(F.data == "download_more")
async def callback_download_more(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("👇 Пришли мне следующую ссылку!", reply_markup=get_main_menu(callback.from_user.id))

# --- ОБРАБОТКА ОСТАЛЬНОГО ТЕКСТА ---
@dp.message(F.text)
async def handle_unknown(message: types.Message):
    await message.answer("Я не понимаю это сообщение. Пришли мне ссылку на видео или используй меню 👇")

# --- ФОНОВАЯ ЗАДАЧА: ПРОВЕРКА СПОНСОРА ---
async def check_sponsor_expiration():
    while True:
        if ad_settings.get("expires_at") and datetime.now().timestamp() > ad_settings["expires_at"]:
            # Срок истек
            uid = ad_settings.get("user_id")
            if uid:
                try:
                    await bot.send_message(uid, "📉 <b>Срок действия спонсорской рекламы истек.</b>\nСпасибо за сотрудничество! Вы можете продлить её в меню '💼 Реклама'.")
                except:
                    pass
            
            # Сбрасываем настройки
            ad_settings["sponsor_text"] = None
            ad_settings["sponsor_link"] = None
            ad_settings["expires_at"] = None
            ad_settings["user_id"] = None
            save_ad_settings()
            
            try:
                await bot.send_message(ADMIN_ID, "ℹ️ <b>Инфо:</b> Срок спонсора истек. Место свободно.")
            except:
                pass
                
        await asyncio.sleep(60) # Проверяем каждую минуту

async def main():
    # Создаем папку для загрузок, если нет
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    
    print("\n\n✅✅✅ ЗАПУЩЕНА НОВАЯ ВЕРСИЯ БОТА (С КНОПКАМИ) ✅✅✅\n\n")
    try:
        # Эта строчка удаляет все сообщения, которые прислали боту, пока он был офлайн
        # Это полезно, чтобы бот не начал спамить ответами на старые команды при включении
        await bot.delete_webhook(drop_pending_updates=True)
        
        me = await bot.get_me()
        print(f"✅ Бот @{me.username} успешно запущен!")
        
        # Запускаем фоновую задачу проверки спонсора
        asyncio.create_task(check_sponsor_expiration())
        
        await dp.start_polling(bot)
    except TelegramConflictError:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Бот уже запущен в другом месте (на ПК или вторая копия на сервере)!")
        print("🛑 Остановите другие копии бота, чтобы этот экземпляр мог работать.")
    except Exception as e:
        print(f"ОШИБКА ПРИ ЗАПУСКЕ: {e}")
        # --- СИСТЕМА 24/7 (АВТО-ПЕРЕЗАПУСК) ---
        while True:
            try:
                await dp.start_polling(bot)
            except TelegramConflictError:
                print("❌ КОНФЛИКТ СЕССИЙ: Бот запущен где-то еще. Повтор через 10 сек...")
                await asyncio.sleep(10)
            except Exception as e:
                print(f"⚠️ КРИТИЧЕСКАЯ ОШИБКА: {e}")
                print("🔄 Перезапуск бота через 5 секунд...")
                await asyncio.sleep(5)
    except KeyboardInterrupt:
        print("Бот остановлен пользователем")

if __name__ == "__main__":
    try:
        keep_alive()  # <--- Добавь это здесь
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен пользователем")