import os
import io
import base64
import secrets
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    print("[IMG] HEIC поддержка включена", flush=True)
except Exception as e:
    print(f"[IMG] HEIC поддержка недоступна: {e}", flush=True)
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'garden_secret_key_change_me')
app.permanent_session_lifetime = timedelta(days=365)

DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', '')
CRON_SECRET = os.environ.get('CRON_SECRET', 'change-me-cron-secret')


# ---------- Работа с БД ----------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        db.autocommit = True
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(sql, args)
        result = cur.fetchall()
        return (result[0] if result else None) if one else result
    finally:
        cur.close()


def execute(sql, args=(), returning=False):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(sql, args)
        if returning and cur.description is not None:
            return cur.fetchone()
        return None
    finally:
        cur.close()


# ---------- Telegram API ----------
def send_telegram_message(chat_id, text, reply_markup=None):
    """Отправка сообщения. reply_markup — словарь с inline_keyboard или др."""
    if not TELEGRAM_BOT_TOKEN:
        print("[TG] TELEGRAM_BOT_TOKEN не задан", flush=True)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        print(f"[TG] Ошибка отправки: {r.status_code} — {r.text[:300]}", flush=True)
        return False
    except Exception as e:
        print(f"[TG] Исключение при отправке: {e}", flush=True)
        return False


def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    """Редактирует существующее сообщение."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[TG] Ошибка редактирования: {e}", flush=True)
        return False


def answer_callback(callback_query_id, text="", show_alert=False):
    """Обязательный ответ на нажатие inline-кнопки."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }, timeout=10)
    except Exception as e:
        print(f"[TG] Ошибка answerCallback: {e}", flush=True)


def get_user_by_telegram(chat_id):
    return query('SELECT * FROM users WHERE telegram_id = %s', (chat_id,), one=True)


def build_tasks_reply(user):
    """Собирает текст и inline-кнопки для задач на сегодня."""
    tasks = get_today_tasks(user['id'])
    if not tasks:
        return f"🌿 {user['name'] or 'Садовод'}, сегодня задач нет. Отдыхай!", None

    lines = [f"🌿 {user['name'] or 'Садовод'}, задачи на сегодня:\n"]
    keyboard = []
    for task in tasks[:8]:  # ограничим 8 задач, чтобы не раздувать сообщение
        icon = {'water': '💧', 'feed': '🧪', 'transplant': '🌱', 'harvest': '🧺'}.get(task['type'], '•')
        lines.append(f"{icon} {task['message']}")
        btn_text = {
            'water': f"✅ Полить {task['plant_name']}",
            'feed': f"✅ Подкормить {task['plant_name']}",
            'transplant': f"✅ Пересадить {task['plant_name']}",
            'harvest': f"✅ Собрать {task['plant_name']}",
        }.get(task['type'], f"✅ {task['plant_name']}")
        keyboard.append([{
            "text": btn_text,
            "callback_data": f"{task['type']}:{task['plant_id']}"
        }])

    reply_markup = {"inline_keyboard": keyboard}
    return "\n".join(lines), reply_markup


def build_garden_reply(user):
    """Собирает текст со списком растений."""
    plants = get_user_plants(user['id'])
    if not plants:
        return "🌱 У тебя пока нет растений. Добавь первое в приложении!"

    lines = ["🌿 Твои растения:\n"]
    for p in plants[:15]:
        water_status = p['last_watered'] or 'ещё не поливали'
        feed_status = p['last_fed'] or 'ещё не подкармливали'
        lines.append(f"• <b>{p['name']}</b>")
        lines.append(f"  💧 {water_status}")
        lines.append(f"  🧪 {feed_status}")
    return "\n".join(lines)


def build_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📋 Задачи на сегодня", "callback_data": "menu:tasks"}],
            [{"text": "🌿 Мои растения", "callback_data": "menu:garden"}],
            [{"text": "❓ Помощь", "callback_data": "menu:help"}],
        ]
    }


# ---------- Инициализация БД ----------
_db_initialized = False


def init_db():
    global _db_initialized
    if _db_initialized:
        return

    with app.app_context():
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE,
                    password_hash TEXT,
                    email TEXT UNIQUE,
                    name TEXT,
                    gender TEXT,
                    experience TEXT,
                    growing_plants TEXT,
                    location TEXT,
                    priority TEXT,
                    theme TEXT DEFAULT 'green',
                    install_banner_closed INTEGER DEFAULT 0,
                    city TEXT,
                    lat REAL,
                    lon REAL,
                    current_streak INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    last_active_date DATE,
                    telegram_id BIGINT UNIQUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS plants_catalog (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    plant_type TEXT,
                    watering_frequency INTEGER DEFAULT 1,
                    feeding_frequency INTEGER DEFAULT 14,
                    transplant_days INTEGER DEFAULT 30,
                    harvest_days INTEGER DEFAULT 60
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_plants (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    plant_id INTEGER REFERENCES plants_catalog(id),
                    planted_date DATE,
                    location TEXT,
                    last_watered DATE,
                    last_fed DATE,
                    notes TEXT,
                    photo TEXT,
                    water_interval_override INTEGER,
                    feed_interval_override INTEGER
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS garden_log (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    plant_id INTEGER REFERENCES user_plants(id),
                    action TEXT,
                    action_date DATE,
                    note TEXT
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    token TEXT UNIQUE,
                    expires_at TIMESTAMP
                )
            ''')
        finally:
            cur.close()

        for sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS city TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lat REAL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lon REAL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS current_streak INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_date DATE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_id BIGINT UNIQUE",
        ]:
            try:
                execute(sql)
            except Exception as e:
                print(f"[MIGRATE] {e}", flush=True)

        row = query('SELECT COUNT(*) AS c FROM plants_catalog', one=True)
        if row and row['c'] == 0:
            catalog = [
                ('Огурцы', 'овощи', 1, 10, 25, 50),
                ('Помидоры', 'овощи', 2, 14, 30, 70),
                ('Перцы', 'овощи', 2, 14, 30, 80),
                ('Баклажаны', 'овощи', 2, 14, 30, 80),
                ('Кабачки', 'овощи', 1, 10, 25, 45),
                ('Морковь', 'корнеплоды', 2, 20, 0, 80),
                ('Свёкла', 'корнеплоды', 2, 20, 0, 80),
                ('Лук', 'корнеплоды', 3, 15, 0, 90),
                ('Укроп', 'зелень', 1, 7, 0, 30),
                ('Петрушка', 'зелень', 2, 10, 0, 40),
                ('Салат', 'зелень', 1, 7, 0, 30),
                ('Клубника', 'ягоды', 2, 20, 0, 120),
                ('Малина', 'ягоды', 2, 20, 0, 150),
                ('Капуста', 'овощи', 2, 14, 30, 90),
                ('Розы', 'цветы', 2, 14, 0, 90),
                ('Тюльпаны', 'цветы', 1, 7, 0, 30),
            ]
            for name, ptype, wf, ff, td, hd in catalog:
                execute(
                    'INSERT INTO plants_catalog (name, plant_type, watering_frequency, feeding_frequency, transplant_days, harvest_days) VALUES (%s,%s,%s,%s,%s,%s)',
                    (name, ptype, wf, ff, td, hd))

        _db_initialized = True


@app.before_request
def before_request():
    init_db()
    session.permanent = True


# ---------- Отправка почты через Brevo ----------
def send_reset_email(to_email, reset_link):
    BREVO_API_KEY = os.environ.get('BREVO_API_KEY')
    BREVO_SENDER_EMAIL = os.environ.get('BREVO_SENDER_EMAIL', 'garden.app@yandex.ru')
    BREVO_SENDER_NAME = os.environ.get('BREVO_SENDER_NAME', 'Мой сад')

    if not BREVO_API_KEY:
        print("[MAIL] BREVO_API_KEY не задан", flush=True)
        return False

    html = f"""
    <html>
    <body style="font-family: Segoe UI, sans-serif; color: #1b5e20; background: #e8f5e9; padding: 20px;">
        <div style="max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; padding: 24px;">
            <h2 style="color: #2e7d32; margin-top: 0;">🌿 Мой сад</h2>
            <p>Привет!</p>
            <p>Ты запросил сброс пароля в приложении «Мой сад».</p>
            <p style="margin: 24px 0; text-align: center;">
                <a href="{reset_link}" style="background: #4caf50; color: white; padding: 14px 24px; border-radius: 12px; text-decoration: none; font-weight: bold; display: inline-block;">
                    Сбросить пароль
                </a>
            </p>
            <p style="font-size: 0.9em; color: #666;">Если кнопка не работает, скопируй ссылку:<br>
                <a href="{reset_link}" style="color: #2e7d32; word-break: break-all;">{reset_link}</a>
            </p>
            <p style="color: #888; font-size: 0.9em;">Ссылка действительна 15 минут.</p>
            <p style="color: #888; font-size: 0.9em;">Если ты не запрашивал сброс, просто проигнорируй это письмо.</p>
            <p>С заботой, Мой сад 🌱</p>
        </div>
    </body>
    </html>
    """

    try:
        print(f"[MAIL] Отправляю письмо через Brevo на {to_email}...", flush=True)
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json"
            },
            json={
                "sender": {
                    "name": BREVO_SENDER_NAME,
                    "email": BREVO_SENDER_EMAIL
                },
                "to": [{"email": to_email}],
                "subject": "Сброс пароля — Мой сад",
                "htmlContent": html
            },
            timeout=15
        )
        if response.status_code in (200, 201, 202):
            print("[MAIL] Письмо успешно отправлено через Brevo", flush=True)
            return True
        else:
            print(f"[MAIL] Ошибка Brevo: {response.status_code} — {response.text}", flush=True)
            return False
    except Exception as e:
        print(f"[MAIL] Ошибка отправки: {e}", flush=True)
        return False


# ---------- Погода ----------
WMO_CODES = {
    0: "☀️ Ясно", 1: "🌤 Малооблачно", 2: "⛅ Облачно", 3: "☁️ Пасмурно",
    45: "🌫 Туман", 48: "🌫 Туман",
    51: "🌦 Морось", 53: "🌦 Морось", 55: "🌦 Морось",
    61: "🌧 Дождь", 63: "🌧 Дождь", 65: "🌧 Сильный дождь",
    71: "🌨 Снег", 73: "🌨 Снег", 75: "🌨 Сильный снег",
    80: "🌦 Ливень", 81: "🌦 Ливень", 82: "🌧 Сильный ливень",
    95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза",
}

WW_CODES = {
    113: "☀️ Ясно", 116: "🌤 Переменная облачность", 119: "⛅ Облачно", 122: "☁️ Пасмурно",
    143: "🌫 Туман", 176: "🌦 Местами дождь", 179: "🌨 Местами снег", 182: "🌨 Местами мокрый снег",
    185: "🌧 Морось", 200: "⛈ Гроза", 227: "🌨 Позёмок", 230: "🌨 Метель",
    248: "🌫 Туман", 260: "🌫 Ледяной туман", 263: "🌦 Мелкая морось", 266: "🌦 Морось",
    281: "🌧 Ледяная морось", 284: "🌧 Сильная ледяная морось",
    293: "🌦 Местами слабый дождь", 296: "🌦 Слабый дождь", 299: "🌧 Умеренный дождь",
    302: "🌧 Умеренный дождь", 305: "🌧 Сильный дождь", 308: "🌧 Сильный дождь",
    311: "🌧 Ледяной дождь", 314: "🌧 Сильный ледяной дождь",
    317: "🌨 Мокрый снег", 320: "🌨 Сильный мокрый снег",
    323: "🌨 Местами слабый снег", 326: "🌨 Слабый снег", 329: "🌨 Умеренный снег",
    332: "🌨 Умеренный снег", 335: "🌨 Сильный снег", 338: "🌨 Сильный снег",
    350: "🌨 Ледяная крупа", 353: "🌦 Небольшой ливень", 356: "🌧 Сильный ливень",
    359: "🌧 Проливной дождь", 362: "🌨 Ливень с мокрым снегом",
    365: "🌨 Сильный ливень с мокрым снегом", 368: "🌨 Небольшой снегопад",
    371: "🌨 Сильный снегопад", 374: "🌨 Ливень с ледяной крупой",
    377: "🌨 Сильный ливень с ледяной крупой", 386: "⛈ Дождь с грозой",
    389: "⛈ Сильный дождь с грозой", 392: "⛈ Снег с грозой", 395: "⛈ Сильный снег с грозой",
}


def geocode_city(city):
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru"},
            timeout=10
        )
        data = r.json()
        if data.get("results"):
            res = data["results"][0]
            return res["latitude"], res["longitude"]
    except Exception as e:
        print(f"[GEOCODE] Ошибка: {e}", flush=True)
    return None, None


def get_weather_forecast(lat, lon):
    try:
        lat_f = float(lat)
        lon_f = float(lon)
        url = f"https://wttr.in/{lat_f},{lon_f}?format=j1&lang=ru"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GardenApp/1.0)"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"[WEATHER] wttr.in HTTP {r.status_code}", flush=True)
            return []
        data = r.json()
        forecast = data.get("weather") or []
        if not forecast:
            return []
        days = []
        for item in forecast[:3]:
            hourly = item.get("hourly") or []
            code = 0
            precip = 0
            if hourly:
                midday = hourly[len(hourly) // 2]
                try:
                    code = int(midday.get("weatherCode", 0))
                except (TypeError, ValueError):
                    code = 0
                for h in hourly:
                    try:
                        precip += float(h.get("precipMM", 0) or 0)
                    except (TypeError, ValueError):
                        pass
            days.append({
                "date": item.get("date"),
                "tmax": float(item.get("maxtempC", 0)),
                "tmin": float(item.get("mintempC", 0)),
                "precip": round(precip, 1),
                "code": code,
                "desc": WW_CODES.get(code, "❓"),
            })
        return days
    except Exception as e:
        print(f"[WEATHER] wttr.in исключение: {e}", flush=True)
        return []


def get_weather_advice(days):
    if not days:
        return []
    advice = []
    today = days[0] if len(days) > 0 else None
    tomorrow = days[1] if len(days) > 1 else None
    if tomorrow and tomorrow["precip"] and tomorrow["precip"] > 3:
        advice.append("🌧 Завтра дождь — можно не поливать")
    if today and today["tmax"] is not None and today["tmax"] > 30:
        advice.append("🔥 Жара — полей растения дважды")
    if today and today["tmin"] is not None and today["tmin"] < 5:
        advice.append("❄️ Холодная ночь — укрой теплолюбивые растения")
    if today and today["tmax"] is not None and today["tmax"] < 10:
        advice.append("🥶 Холодно — поливай реже обычного")
    return advice


def _decline_simple(word):
    if not word or len(word) < 2:
        return word
    last = word[-1].lower()
    if last in ('о', 'е', 'и', 'у', 'ю', 'ы', 'э'):
        return word
    if word[-2:].lower() == 'ия':
        return word[:-1] + 'и'
    if word[-2:].lower() == 'ья':
        return word[:-1] + 'е'
    if last == 'а':
        return word[:-1] + 'е'
    if last == 'я':
        return word[:-1] + 'е'
    if last == 'ь':
        return word[:-1] + 'и'
    return word + 'е'


def city_to_prepositional(city):
    if not city:
        return city
    c = city.strip()
    if len(c) < 2:
        return c
    if '-' in c:
        parts = c.split('-')
        if any(p and p[0].islower() for p in parts):
            return c
        parts[-1] = _decline_simple(parts[-1])
        return '-'.join(parts)
    if ' ' in c:
        parts = c.split(' ')
        parts[-1] = _decline_simple(parts[-1])
        return ' '.join(parts)
    return _decline_simple(c)


# ---------- Streak ----------
def update_streak(user_id):
    user = query('SELECT current_streak, best_streak, last_active_date FROM users WHERE id = %s',
                 (user_id,), one=True)
    if not user:
        return
    today = date.today()
    last = user['last_active_date']
    if isinstance(last, str):
        last = datetime.strptime(last[:10], '%Y-%m-%d').date()
    current = user['current_streak'] or 0
    best = user['best_streak'] or 0
    if last == today:
        return
    if last and (today - last).days == 1:
        current += 1
    else:
        current = 1
    if current > best:
        best = current
    execute('UPDATE users SET current_streak=%s, best_streak=%s, last_active_date=%s WHERE id=%s',
            (current, best, today.isoformat(), user_id))


# ---------- Вспомогательные ----------
def get_user():
    if 'user_id' in session:
        return query('SELECT * FROM users WHERE id = %s', (session['user_id'],), one=True)
    return None


def get_catalog():
    return query('SELECT * FROM plants_catalog ORDER BY name')


def get_user_plants(user_id):
    return query('''
        SELECT up.*, pc.name, pc.plant_type, pc.watering_frequency, pc.feeding_frequency,
               pc.transplant_days, pc.harvest_days
        FROM user_plants up
        JOIN plants_catalog pc ON up.plant_id = pc.id
        WHERE up.user_id = %s
        ORDER BY up.planted_date DESC
    ''', (user_id,))


def get_today_tasks(user_id):
    plants = get_user_plants(user_id)
    today = date.today()
    tasks = []
    for p in plants:
        water_interval = p['water_interval_override'] or p['watering_frequency']
        feed_interval = p['feed_interval_override'] or p['feeding_frequency']

        if water_interval and water_interval > 0:
            last_w = p['last_watered']
            if last_w is None:
                tasks.append({'type': 'water', 'plant_name': p['name'], 'plant_id': p['id'],
                              'message': f'Пора полить {p["name"]}'})
            else:
                if isinstance(last_w, str):
                    last_w = datetime.strptime(last_w[:10], '%Y-%m-%d').date()
                if (today - last_w).days >= water_interval:
                    tasks.append({'type': 'water', 'plant_name': p['name'], 'plant_id': p['id'],
                                  'message': f'Пора полить {p["name"]}'})

        if feed_interval and feed_interval > 0:
            last_f = p['last_fed'] if p['last_fed'] else p['planted_date']
            if isinstance(last_f, str):
                last_f = datetime.strptime(last_f[:10], '%Y-%m-%d').date()
            if (today - last_f).days >= feed_interval:
                tasks.append({'type': 'feed', 'plant_name': p['name'], 'plant_id': p['id'],
                              'message': f'Пора подкормить {p["name"]}'})

        if p['transplant_days'] and p['transplant_days'] > 0:
            planted = p['planted_date']
            if isinstance(planted, str):
                planted = datetime.strptime(planted[:10], '%Y-%m-%d').date()
            if (today - planted).days >= p['transplant_days']:
                tasks.append({'type': 'transplant', 'plant_name': p['name'], 'plant_id': p['id'],
                              'message': f'Пора пересадить {p["name"]}'})

        if p['harvest_days'] and p['harvest_days'] > 0:
            planted = p['planted_date']
            if isinstance(planted, str):
                planted = datetime.strptime(planted[:10], '%Y-%m-%d').date()
            if (today - planted).days >= p['harvest_days']:
                tasks.append({'type': 'harvest', 'plant_name': p['name'], 'plant_id': p['id'],
                              'message': f'Можно собирать урожай {p["name"]}'})
    return tasks


def get_calendar_events(user_id, year, month):
    plants = get_user_plants(user_id)
    events = defaultdict(list)
    for p in plants:
        planted = p['planted_date']
        if isinstance(planted, str):
            planted = datetime.strptime(planted[:10], '%Y-%m-%d').date()
        if planted.year == year and planted.month == month:
            events[planted.day].append({'title': f'Посадка: {p["name"]}', 'type': 'planting'})
        if p['transplant_days'] and p['transplant_days'] > 0:
            trans_date = planted + timedelta(days=p['transplant_days'])
            if trans_date.year == year and trans_date.month == month:
                events[trans_date.day].append({'title': f'Пересадка: {p["name"]}', 'type': 'transplant'})
        if p['harvest_days'] and p['harvest_days'] > 0:
            harv_date = planted + timedelta(days=p['harvest_days'])
            if harv_date.year == year and harv_date.month == month:
                events[harv_date.day].append({'title': f'Урожай: {p["name"]}', 'type': 'harvest'})
    return dict(events)


CARE_TIPS = {
    'Огурцы': 'Поливай каждый день, особенно в жару. Подкармливай каждые 10 дней органическим удобрением.',
    'Помидоры': 'Поливай раз в 2 дня под корень, не попадая на листья. Подкармливай раз в 2 недели комплексным удобрением.',
    'Перцы': 'Поливай раз в 2 дня, любит тёплую воду. Подкармливай раз в 2 недели фосфорно-калийными удобрениями.',
    'Баклажаны': 'Поливай раз в 2 дня, не допускай пересыхания. Подкармливай раз в 2 недели.',
    'Кабачки': 'Поливай ежедневно, обильно. Подкармливай раз в 10 дней настоем коровяка.',
    'Морковь': 'Поливай раз в 2-3 дня, после прореживания. Подкармливай раз в 3 недели золой или калийными удобрениями.',
    'Свёкла': 'Поливай раз в 2-3 дня. Подкармливай раз в 3 недели комплексным удобрением.',
    'Лук': 'Поливай раз в 3 дня, за 2 недели до уборки полив прекрати. Подкармливай раз в 3 недели.',
    'Укроп': 'Поливай ежедневно. Подкармливай раз в 7 дней азотными удобрениями.',
    'Петрушка': 'Поливай раз в 2 дня. Подкармливай раз в 10 дней.',
    'Салат': 'Поливай ежедневно, любит влажную почву. Подкармливай раз в 7 дней.',
    'Клубника': 'Поливай раз в 2 дня, не заливая ягоды. Подкармливай раз в 3 недели.',
    'Малина': 'Поливай раз в 2 дня. Подкармливай раз в 3 недели органическими удобрениями.',
    'Капуста': 'Поливай раз в 2 дня, обильно. Подкармливай раз в 2 недели азотными удобрениями.',
    'Розы': 'Поливай раз в 2 дня, не попадая на листья. Подкармливай раз в 2 недели специальным удобрением для роз.',
    'Тюльпаны': 'Поливай раз в 2-3 дня. Подкармливай раз в неделю в период роста.'
}


# ---------- Обработчики Telegram ----------
def handle_telegram_message(message):
    """Обработка входящего текстового сообщения."""
    text = (message.get('text') or '').strip()
    chat = message.get('chat') or {}
    chat_id = chat.get('id')

    if not chat_id or not text:
        return

    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    if '@' in command:
        command = command.split('@')[0]

    if command == '/start':
        if len(parts) > 1:
            app_username = parts[1].strip().lower()
            user = query('SELECT id, name FROM users WHERE LOWER(username) = %s',
                         (app_username,), one=True)
            if user:
                execute('UPDATE users SET telegram_id = %s WHERE id = %s', (chat_id, user['id']))
                name = user['name'] or app_username
                send_telegram_message(
                    chat_id,
                    f"Привет, {name}! 🌿\n\n"
                    f"Теперь я буду присылать тебе напоминания о поливе и подкормке.\n"
                    f"Каждое утро в 8:00 жди сообщение с задачами.\n\n"
                    f"Вот меню:",
                    reply_markup=build_menu_keyboard()
                )
                print(f"[TG] Привязан chat_id={chat_id} к user_id={user['id']}", flush=True)
                return
            else:
                send_telegram_message(
                    chat_id,
                    f"Не нашёл пользователя с логином «{app_username}».\n"
                    f"Проверь логин в профиле приложения."
                )
                return
        send_telegram_message(
            chat_id,
            "Привет! 🌿 Я бот приложения «Мой сад».\n\n"
            "Открой приложение и нажми «Подключить Telegram» в профиле.",
        )
        return

    # Для остальных команд нужен привязанный пользователь
    user = get_user_by_telegram(chat_id)

    if command == '/stop':
        execute('UPDATE users SET telegram_id = NULL WHERE telegram_id = %s', (chat_id,))
        send_telegram_message(
            chat_id,
            "Хорошо, больше не буду присылать напоминания. Если захочешь вернуть — напиши /start."
        )
        return

    if command == '/help':
        send_telegram_message(
            chat_id,
            "🌿 Бот приложения «Мой сад»\n\n"
            "Команды:\n"
            "/start — приветствие и привязка\n"
            "/tasks — задачи на сегодня\n"
            "/garden — список растений\n"
            "/stop — отключить напоминания\n"
            "/help — эта справка"
        )
        return

    if not user:
        send_telegram_message(
            chat_id,
            "Твой Telegram ещё не привязан к аккаунту. Открой приложение → Профиль → «Подключить Telegram»."
        )
        return

    if command == '/tasks':
        text_msg, keyboard = build_tasks_reply(user)
        send_telegram_message(chat_id, text_msg, reply_markup=keyboard)
        return

    if command == '/garden':
        send_telegram_message(chat_id, build_garden_reply(user))
        return

    send_telegram_message(chat_id, "Не понимаю эту команду. Напиши /help, чтобы увидеть список команд.")


def handle_telegram_callback(cb):
    """Обработка нажатия inline-кнопки."""
    cb_id = cb.get('id')
    data = cb.get('data', '')
    message = cb.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    message_id = message.get('message_id')

    if not chat_id or not data:
        if cb_id:
            answer_callback(cb_id)
        return

    user = get_user_by_telegram(chat_id)
    if not user:
        answer_callback(cb_id, "Аккаунт не привязан. Открой приложение → Профиль.", show_alert=True)
        return

    # Кнопки меню
    if data == 'menu:tasks':
        text_msg, keyboard = build_tasks_reply(user)
        send_telegram_message(chat_id, text_msg, reply_markup=keyboard)
        answer_callback(cb_id)
        return

    if data == 'menu:garden':
        send_telegram_message(chat_id, build_garden_reply(user))
        answer_callback(cb_id)
        return

    if data == 'menu:help':
        send_telegram_message(
            chat_id,
            "🌿 Бот приложения «Мой сад»\n\n"
            "Нажми на кнопки ниже, чтобы посмотреть задачи или растения.\n"
            "Команды: /tasks, /garden, /stop, /help"
        )
        answer_callback(cb_id)
        return

    # Кнопки действий с растениями: water:1, feed:1, transplant:1, harvest:1
    if ':' in data:
        action, plant_id_str = data.split(':', 1)
        try:
            plant_id = int(plant_id_str)
        except ValueError:
            answer_callback(cb_id, "Ошибка данных")
            return

        plant = query('''SELECT up.*, pc.name FROM user_plants up
                         JOIN plants_catalog pc ON up.plant_id = pc.id
                         WHERE up.id = %s AND up.user_id = %s''',
                      (plant_id, user['id']), one=True)
        if not plant:
            answer_callback(cb_id, "Растение не найдено", show_alert=True)
            return

        today_iso = date.today().isoformat()

        if action == 'water':
            execute('UPDATE user_plants SET last_watered = %s WHERE id = %s AND user_id = %s',
                    (today_iso, plant_id, user['id']))
            update_streak(user['id'])
            answer_callback(cb_id, f"💧 {plant['name']} полито!")
            confirm = f"✅ <b>{plant['name']}</b> — полито {today_iso}"
        elif action == 'feed':
            execute('UPDATE user_plants SET last_fed = %s WHERE id = %s AND user_id = %s',
                    (today_iso, plant_id, user['id']))
            update_streak(user['id'])
            answer_callback(cb_id, f"🧪 {plant['name']} подкормлено!")
            confirm = f"✅ <b>{plant['name']}</b> — подкормлено {today_iso}"
        elif action == 'transplant':
            execute('INSERT INTO garden_log (user_id, plant_id, action, action_date, note) VALUES (%s,%s,%s,%s,%s)',
                    (user['id'], plant_id, 'пересадка', today_iso, 'Пересажено через Telegram'))
            update_streak(user['id'])
            answer_callback(cb_id, f"🌱 {plant['name']} пересажено!")
            confirm = f"✅ <b>{plant['name']}</b> — пересажено {today_iso}"
        elif action == 'harvest':
            execute('INSERT INTO garden_log (user_id, plant_id, action, action_date, note) VALUES (%s,%s,%s,%s,%s)',
                    (user['id'], plant_id, 'урожай', today_iso, 'Собрано через Telegram'))
            update_streak(user['id'])
            answer_callback(cb_id, f"🧺 Урожай {plant['name']} собран!")
            confirm = f"✅ <b>{plant['name']}</b> — урожай собран {today_iso}"
        else:
            answer_callback(cb_id, "Неизвестное действие")
            return

        # Обновляем текст сообщения — убираем нажатую кнопку
        old_text = message.get('text', '') or ''
        new_text = old_text + f"\n\n{confirm}"
        # Убираем старые кнопки, оставляем только невыполненные — упрощённо: убираем все
        # (более сложная логика — обновлять отдельные кнопки — избыточна для MVP)
        # Пересобираем задачи заново
        new_text2, new_keyboard = build_tasks_reply(user)
        # Если есть ещё задачи — показываем новое меню, если нет — просто подтверждение
        if new_keyboard:
            edit_telegram_message(
                chat_id, message_id,
                new_text2 + f"\n\n{confirm}",
                reply_markup=new_keyboard
            )
        else:
            edit_telegram_message(
                chat_id, message_id,
                f"{confirm}\n\n🎉 Все задачи на сегодня выполнены!"
            )


# ---------- Роуты ----------
@app.route('/healthz')
def healthz():
    return 'ok', 200


@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Принимает обновления от Telegram."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        print(f"[TG] Webhook: {str(data)[:500]}", flush=True)

        if 'message' in data or 'edited_message' in data:
            msg = data.get('message') or data.get('edited_message')
            handle_telegram_message(msg)

        elif 'callback_query' in data:
            cb = data['callback_query']
            handle_telegram_callback(cb)

        return 'ok', 200
    except Exception as e:
        print(f"[TG] Ошибка webhook: {type(e).__name__}: {e}", flush=True)
        return 'ok', 200


@app.route('/cron/send_reminders', methods=['POST', 'GET'])
def cron_send_reminders():
    secret = request.args.get('secret', '')
    if secret != CRON_SECRET:
        return 'Forbidden', 403

    users = query('SELECT * FROM users WHERE telegram_id IS NOT NULL')
    if not users:
        return 'No users with Telegram', 200

    sent = 0
    for u in users:
        text, keyboard = build_tasks_reply(u)
        if not text:
            continue
        text = f"🌿 Доброе утро, {u['name'] or 'садовод'}!\n\n" + text
        if send_telegram_message(u['telegram_id'], text, reply_markup=keyboard):
            sent += 1

    print(f"[TG] Отправлено напоминаний: {sent}", flush=True)
    return f'Sent: {sent}', 200


@app.route('/')
def splash():
    user = get_user()
    return render_template('splash.html', user=user)


@app.route('/dashboard')
def dashboard():
    user = get_user()
    if not user:
        return redirect(url_for('login'))

    if user['city'] and (not user['lat'] or not user['lon']):
        lat, lon = geocode_city(user['city'])
        if lat and lon:
            execute('UPDATE users SET lat=%s, lon=%s WHERE id=%s', (lat, lon, user['id']))
            user = get_user()
            print(f"[GEOCODE] Город '{user['city']}' → {lat}, {lon}", flush=True)

    tasks = get_today_tasks(user['id'])
    plants_count = len(get_user_plants(user['id']))

    weather = []
    weather_advice = []
    if user['lat'] and user['lon']:
        weather = get_weather_forecast(user['lat'], user['lon'])
        weather_advice = get_weather_advice(weather)

    city_in_case = city_to_prepositional(user['city']) if user['city'] else None

    return render_template('index.html', user=user, tasks=tasks, plants_count=plants_count,
                           weather=weather, weather_advice=weather_advice,
                           city_in_case=city_in_case)


@app.route('/close_install_banner', methods=['POST'])
def close_install_banner():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE users SET install_banner_closed = 1 WHERE id = %s', (user['id'],))
    return jsonify({'success': True})


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not username or len(password) < 4:
            flash('Логин обязателен, пароль от 4 символов', 'error')
            return render_template('register.html')
        if not email or '@' not in email:
            flash('Укажи корректный email — он нужен для восстановления пароля', 'error')
            return render_template('register.html')

        existing = query('SELECT id FROM users WHERE username = %s OR email = %s', (username, email), one=True)
        if existing:
            flash('Такой логин или email уже занят', 'error')
            return render_template('register.html')

        password_hash = generate_password_hash(password)
        new_user = execute('INSERT INTO users (username, email, password_hash) VALUES (%s,%s,%s) RETURNING id',
                           (username, email, password_hash), returning=True)
        session['user_id'] = new_user['id']
        return redirect(url_for('onboarding'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = query('SELECT * FROM users WHERE username = %s', (username,), one=True)
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            return redirect(url_for('dashboard'))
        else:
            flash('Неверный логин или пароль', 'error')
    return render_template('login.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email or '@' not in email:
            flash('Введи корректный email', 'error')
            return render_template('forgot_password.html')

        user = query('SELECT * FROM users WHERE email = %s', (email,), one=True)
        if user:
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.utcnow() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            execute('DELETE FROM password_reset_tokens WHERE user_id = %s', (user['id'],))
            execute('INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s,%s,%s)',
                    (user['id'], token, expires_at))
            reset_link = url_for('reset_password', token=token, _external=True)
            if send_reset_email(email, reset_link):
                flash('Ссылка для сброса отправлена на почту', 'success')
            else:
                flash('Не удалось отправить письмо. Попробуй позже.', 'error')
                return render_template('forgot_password.html')
        else:
            flash('Если такой email зарегистрирован — мы отправили на него ссылку.', 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    reset = query('SELECT * FROM password_reset_tokens WHERE token = %s', (token,), one=True)
    if not reset:
        flash('Недействительная ссылка для сброса пароля', 'error')
        return redirect(url_for('forgot_password'))

    expires_at = reset['expires_at']
    if isinstance(expires_at, str):
        expires_at = datetime.strptime(expires_at[:19], '%Y-%m-%d %H:%M:%S')
    if datetime.utcnow() > expires_at:
        execute('DELETE FROM password_reset_tokens WHERE id = %s', (reset['id'],))
        flash('Срок действия ссылки истёк. Запроси сброс заново.', 'error')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        if len(new_password) < 4:
            flash('Пароль должен быть не короче 4 символов', 'error')
            return render_template('reset_password.html', token=token)

        new_hash = generate_password_hash(new_password)
        execute('UPDATE users SET password_hash = %s WHERE id = %s', (new_hash, reset['user_id']))
        execute('DELETE FROM password_reset_tokens WHERE id = %s', (reset['id'],))
        flash('Пароль успешно изменён! Теперь войди с новым паролем.', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password.html', token=token)


@app.route('/onboarding', methods=['GET', 'POST'])
def onboarding():
    user = get_user()
    if not user:
        return redirect(url_for('register'))
    if request.method == 'POST':
        data = request.form
        name = data.get('name', '').strip()
        gender = data.get('gender', '')
        experience = data.get('experience', '')
        growing_plants = ','.join(request.form.getlist('plants'))
        location = ','.join(request.form.getlist('locations'))
        priority = ','.join(request.form.getlist('priorities'))
        execute(
            'UPDATE users SET name=%s, gender=%s, experience=%s, growing_plants=%s, location=%s, priority=%s WHERE id=%s',
            (name, gender, experience, growing_plants, location, priority, user['id']))
        return redirect(url_for('dashboard'))
    return render_template('onboarding.html', user=user)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/add_plant', methods=['GET', 'POST'])
def add_plant():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        plant_id = request.form.get('plant_id')
        custom_name = request.form.get('custom_name', '').strip()
        planted_date = request.form.get('planted_date')
        location = request.form.get('location', '')
        notes = request.form.get('notes', '')
        water_interval = request.form.get('water_interval', '')
        feed_interval = request.form.get('feed_interval', '')
        if custom_name:
            new_plant = execute('INSERT INTO plants_catalog (name, plant_type) VALUES (%s,%s) RETURNING id',
                                (custom_name, 'другое'), returning=True)
            plant_id = new_plant['id']
        if not plant_id:
            flash('Выберите растение из списка или укажите своё', 'error')
            return redirect(url_for('add_plant'))
        execute(
            'INSERT INTO user_plants (user_id, plant_id, planted_date, location, notes, water_interval_override, feed_interval_override) VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (user['id'], plant_id, planted_date, location, notes,
             int(water_interval) if water_interval else None,
             int(feed_interval) if feed_interval else None))
        return redirect(url_for('garden'))
    catalog = get_catalog()
    return render_template('add_plant.html', user=user, catalog=catalog, care_tips=CARE_TIPS)


@app.route('/garden')
def garden():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    plants = get_user_plants(user['id'])
    return render_template('garden.html', user=user, plants=plants, care_tips=CARE_TIPS)


@app.route('/plant/<int:plant_id>')
def plant_detail(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    plant = query('''SELECT up.*, pc.name, pc.plant_type, pc.watering_frequency,
                            pc.feeding_frequency, pc.transplant_days, pc.harvest_days
                     FROM user_plants up
                     JOIN plants_catalog pc ON up.plant_id = pc.id
                     WHERE up.id = %s AND up.user_id = %s''',
                  (plant_id, user['id']), one=True)
    if not plant:
        flash('Растение не найдено', 'error')
        return redirect(url_for('garden'))
    logs = query('''SELECT * FROM garden_log
                    WHERE plant_id = %s
                    ORDER BY action_date DESC LIMIT 30''', (plant_id,))
    return render_template('plant_detail.html', user=user, plant=plant, logs=logs, care_tips=CARE_TIPS)


@app.route('/plant/<int:plant_id>/notes', methods=['POST'])
def update_plant_notes(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    notes = request.form.get('notes', '').strip()
    execute('UPDATE user_plants SET notes = %s WHERE id = %s AND user_id = %s',
            (notes, plant_id, user['id']))
    flash('Заметки сохранены', 'success')
    return redirect(url_for('plant_detail', plant_id=plant_id))


@app.route('/plant/<int:plant_id>/photo', methods=['POST'])
def upload_plant_photo(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    file = request.files.get('photo')
    if not file or file.filename == '':
        flash('Файл не выбран', 'error')
        return redirect(url_for('plant_detail', plant_id=plant_id))
    try:
        raw = file.read()
        if not raw:
            flash('Файл пустой', 'error')
            return redirect(url_for('plant_detail', plant_id=plant_id))
        stream = io.BytesIO(raw)
        img = Image.open(stream)
        img.load()
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=80, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        execute('UPDATE user_plants SET photo = %s WHERE id = %s AND user_id = %s',
                (b64, plant_id, user['id']))
        flash('Фото загружено 🌿', 'success')
    except Exception as e:
        print(f"[IMG] Ошибка загрузки: {e}", flush=True)
        flash(f'Ошибка загрузки фото: {e}', 'error')
    return redirect(url_for('plant_detail', plant_id=plant_id))


@app.route('/plant/<int:plant_id>/photo/delete', methods=['POST'])
def delete_plant_photo(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE user_plants SET photo = NULL WHERE id = %s AND user_id = %s',
            (plant_id, user['id']))
    flash('Фото удалено', 'success')
    return redirect(url_for('plant_detail', plant_id=plant_id))


@app.route('/plant/<int:plant_id>/water', methods=['POST'])
def water_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE user_plants SET last_watered = %s WHERE id = %s AND user_id = %s',
            (date.today().isoformat(), plant_id, user['id']))
    update_streak(user['id'])
    return redirect(request.referrer or url_for('garden'))


@app.route('/plant/<int:plant_id>/feed', methods=['POST'])
def feed_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE user_plants SET last_fed = %s WHERE id = %s AND user_id = %s',
            (date.today().isoformat(), plant_id, user['id']))
    update_streak(user['id'])
    return redirect(request.referrer or url_for('garden'))


@app.route('/plant/<int:plant_id>/transplant', methods=['POST'])
def transplant_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('INSERT INTO garden_log (user_id, plant_id, action, action_date, note) VALUES (%s,%s,%s,%s,%s)',
            (user['id'], plant_id, 'пересадка', date.today().isoformat(), 'Пересажено'))
    update_streak(user['id'])
    return redirect(request.referrer or url_for('garden'))


@app.route('/plant/<int:plant_id>/schedule', methods=['POST'])
def update_plant_schedule(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    water_interval = request.form.get('water_interval', '')
    feed_interval = request.form.get('feed_interval', '')
    execute('UPDATE user_plants SET water_interval_override=%s, feed_interval_override=%s WHERE id=%s AND user_id=%s',
            (int(water_interval) if water_interval else None,
             int(feed_interval) if feed_interval else None,
             plant_id, user['id']))
    return redirect(url_for('plant_detail', plant_id=plant_id))


@app.route('/plant/<int:plant_id>/delete', methods=['POST'])
def delete_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('DELETE FROM garden_log WHERE plant_id = %s', (plant_id,))
    execute('DELETE FROM user_plants WHERE id = %s AND user_id = %s', (plant_id, user['id']))
    return redirect(url_for('garden'))


@app.route('/diary')
def diary():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    logs = query('''SELECT gl.*, pc.name as plant_name
                    FROM garden_log gl
                    LEFT JOIN user_plants up ON gl.plant_id = up.id
                    LEFT JOIN plants_catalog pc ON up.plant_id = pc.id
                    WHERE gl.user_id = %s ORDER BY gl.action_date DESC LIMIT 100''', (user['id'],))
    plants = get_user_plants(user['id'])
    return render_template('diary.html', user=user, logs=logs, plants=plants)


@app.route('/diary/add', methods=['POST'])
def add_diary_note():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    plant_id = request.form.get('plant_id')
    note = request.form.get('note', '').strip()
    if plant_id:
        execute('INSERT INTO garden_log (user_id, plant_id, action, action_date, note) VALUES (%s,%s,%s,%s,%s)',
                (user['id'], int(plant_id), 'заметка', date.today().isoformat(), note))
    else:
        execute('INSERT INTO garden_log (user_id, plant_id, action, action_date, note) VALUES (%s,%s,%s,%s,%s)',
                (user['id'], None, 'заметка', date.today().isoformat(), note))
    return redirect(url_for('diary'))


@app.route('/calendar')
def calendar():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    today = date.today()
    events = get_calendar_events(user['id'], today.year, today.month)
    months_ru_nom = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
                     'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
    months_ru_gen = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
                     'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
    month_name_nom = months_ru_nom[today.month - 1]
    month_name_gen = months_ru_gen[today.month - 1]
    return render_template('calendar.html', user=user, events=events,
                           year=today.year, month=today.month,
                           month_name_nom=month_name_nom, month_name_gen=month_name_gen)


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        theme = request.form.get('theme', 'green')
        email = request.form.get('email', '').strip().lower()
        city = request.form.get('city', '').strip()

        if email:
            if '@' not in email:
                flash('Некорректный email', 'error')
                return redirect(url_for('profile'))
            other = query('SELECT id FROM users WHERE email = %s AND id != %s',
                          (email, user['id']), one=True)
            if other:
                flash('Этот email уже привязан к другому аккаунту', 'error')
                return redirect(url_for('profile'))

        old_city = user['city'] or ''
        if city and city != old_city:
            lat, lon = geocode_city(city)
            if lat and lon:
                execute('UPDATE users SET lat=%s, lon=%s WHERE id=%s', (lat, lon, user['id']))
            else:
                flash(f'Не удалось найти город «{city}» — попробуй написать по-другому', 'error')
                city = old_city

        execute('UPDATE users SET name=%s, theme=%s, email=%s, city=%s WHERE id=%s',
                (name, theme, email or None, city or None, user['id']))
        flash('Профиль сохранён', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user,
                           telegram_bot_username=TELEGRAM_BOT_USERNAME)


@app.route('/telegram/disconnect', methods=['POST'])
def telegram_disconnect():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE users SET telegram_id = NULL WHERE id = %s', (user['id'],))
    flash('Telegram отключён', 'success')
    return redirect(url_for('profile'))


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
