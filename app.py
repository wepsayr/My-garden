import os
import secrets
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'garden_secret_key_change_me')
app.permanent_session_lifetime = timedelta(days=365)

DATABASE_URL = os.environ.get('DATABASE_URL')

DATA_DIR = os.environ.get('DATA_DIR', 'data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------- Работа с БД ----------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        db.autocommit = True  # каждый запрос — своя транзакция, ошибка не ломает сессию
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


_db_initialized = False


def init_db():
    """Создаёт таблицы и наполняет каталог. Выполняется один раз за процесс."""
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
    init_db()  # быстрый выход, если уже инициализирован
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


# ---------- Вспомогательные функции ----------
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


# ---------- Маршруты ----------
@app.route('/healthz')
def healthz():
    """Пинг для cron-job / UptimeRobot. Дёшево, без обращения к БД."""
    return 'ok', 200


@app.route('/')
def splash():
    user = get_user()
    return render_template('splash.html', user=user)


@app.route('/dashboard')
def dashboard():
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    tasks = get_today_tasks(user['id'])
    plants_count = len(get_user_plants(user['id']))
    return render_template('index.html', user=user, tasks=tasks, plants_count=plants_count)


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


@app.route('/plant/<int:plant_id>/water', methods=['POST'])
def water_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE user_plants SET last_watered = %s WHERE id = %s AND user_id = %s',
            (date.today().isoformat(), plant_id, user['id']))
    return redirect(url_for('garden'))


@app.route('/plant/<int:plant_id>/feed', methods=['POST'])
def feed_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('UPDATE user_plants SET last_fed = %s WHERE id = %s AND user_id = %s',
            (date.today().isoformat(), plant_id, user['id']))
    return redirect(url_for('garden'))


@app.route('/plant/<int:plant_id>/transplant', methods=['POST'])
def transplant_plant(plant_id):
    user = get_user()
    if not user:
        return redirect(url_for('login'))
    execute('INSERT INTO garden_log (user_id, plant_id, action, action_date, note) VALUES (%s,%s,%s,%s,%s)',
            (user['id'], plant_id, 'пересадка', date.today().isoformat(), 'Пересажено'))
    return redirect(url_for('garden'))


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
    return redirect(url_for('garden'))


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

        if email:
            if '@' not in email:
                flash('Некорректный email', 'error')
                return redirect(url_for('profile'))
            other = query('SELECT id FROM users WHERE email = %s AND id != %s',
                          (email, user['id']), one=True)
            if other:
                flash('Этот email уже привязан к другому аккаунту', 'error')
                return redirect(url_for('profile'))

        execute('UPDATE users SET name=%s, theme=%s, email=%s WHERE id=%s',
                (name, theme, email or None, user['id']))
        flash('Профиль сохранён', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user)


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
