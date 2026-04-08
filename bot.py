import os
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# Попробуем импортировать requests_html для рендеринга JS
try:
    from requests_html import HTMLSession
    HAS_HTML_SESSION = True
except ImportError:
    HAS_HTML_SESSION = False
    logging.warning("requests_html не установлен, рендеринг JS недоступен")

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

# Источники (можно переключать)
GOOGLE_DOC_URL = "https://docs.google.com/document/u/0/d/1ZjBfEvJzmluiZy-5HqvulPHHfqjYTbxltDp4hbCdZWc/pub"
ALF_API_URL = "https://para.alf-kai.ru/?q=24100"
ALF_HTML_URL = "https://alf-kai.ru/расписание/"

GROUP_VARIANTS = ["24100", "09.03.03"]
SEMESTER_START = datetime(2026, 2, 9)
VERSION = "2026-04-08-debug-v2"

# ---------- Кэш ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule()
        _cache["expires"] = now + 3600
    return _cache["data"]

# ---------- Универсальная загрузка ----------
def load_schedule():
    # 1. Пробуем Google Docs
    try:
        logging.info("Пробуем загрузить Google Doc...")
        return parse_google_doc()
    except Exception as e:
        logging.error(f"Google Doc ошибка: {e}")

    # 2. Пробуем alf-kai через requests_html (если есть)
    if HAS_HTML_SESSION:
        try:
            logging.info("Пробуем загрузить alf-kai.ru с рендерингом JS...")
            return parse_alf_with_js()
        except Exception as e:
            logging.error(f"alf-kai JS ошибка: {e}")

    # 3. Fallback на статический HTML alf-kai
    try:
        logging.info("Пробуем статический HTML alf-kai.ru...")
        return parse_alf_html()
    except Exception as e:
        logging.error(f"alf-kai HTML ошибка: {e}")

    raise ValueError("Все источники не дали результата")

# ---------- Парсинг Google Doc ----------
def parse_google_doc():
    resp = requests.get(GOOGLE_DOC_URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    table = soup.find('table')
    if not table:
        raise ValueError("Google Doc: таблица не найдена")
    return parse_html_table(str(table), source="google_doc")

# ---------- Парсинг alf-kai с JS (requests_html) ----------
def parse_alf_with_js():
    session = HTMLSession()
    r = session.get(ALF_HTML_URL)
    # Ждём выполнения JavaScript (увеличьте при необходимости)
    r.html.render(timeout=20, sleep=3)
    # Ищем таблицу
    tables = r.html.find('table')
    if not tables:
        raise ValueError("После рендеринга таблиц не найдено")
    # Выбираем нужную таблицу (обычно с расписанием)
    # Логируем количество и первые строки
    for i, tbl in enumerate(tables):
        logging.info(f"Таблица {i}: {tbl.html[:200]}")
    target_html = tables[0].html  # первая попавшаяся
    return parse_html_table(target_html, source="alf_js")

# ---------- Парсинг статического HTML alf-kai ----------
def parse_alf_html():
    resp = requests.get(ALF_HTML_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    tables = soup.find_all('table')
    if not tables:
        raise ValueError("alf-kai HTML: таблиц нет")
    # Логируем все таблицы для анализа
    for i, tbl in enumerate(tables):
        # Проверяем наличие дней недели в таблице
        tbl_text = tbl.get_text()
        if any(day in tbl_text for day in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница']):
            logging.info(f"Найдена таблица с днями недели (индекс {i})")
            return parse_html_table(str(tbl), source="alf_html")
    raise ValueError("Не найдена таблица с днями недели")

# ---------- Общий парсер HTML таблицы ----------
def parse_html_table(html, source=""):
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.find_all('tr')
    if len(rows) < 2:
        raise ValueError(f"{source}: мало строк в таблице")

    # Логируем шапку для отладки
    header_row = rows[0]
    header_cells = header_row.find_all(['td', 'th'])
    header_texts = [cell.get_text(strip=True) for cell in header_cells]
    logging.info(f"{source} шапка: {header_texts}")

    # Поиск колонки группы
    target_col = None
    for idx, text in enumerate(header_texts):
        for variant in GROUP_VARIANTS:
            if variant in text:
                target_col = idx
                break
        if target_col is not None:
            break

    if target_col is None:
        # Попробуем угадать: часто колонка группы третья (индекс 2)
        if len(header_cells) >= 3:
            target_col = 2
            logging.warning(f"{source}: колонка группы не найдена, берём индекс 2")
        else:
            raise ValueError(f"{source}: не удалось определить колонку группы")

    logging.info(f"{source}: целевая колонка = {target_col}")

    day_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    day_short = {'Понедельник': 'Пн', 'Вторник': 'Вт', 'Среда': 'Ср',
                 'Четверг': 'Чт', 'Пятница': 'Пт', 'Суббота': 'Сб'}

    schedule = {}
    current_day = None
    rowspan_rem = 0

    for row in rows[1:]:
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue

        # Обработка rowspan
        if rowspan_rem == 0:
            day_cell = cells[0]
            day_text = day_cell.get_text(strip=True)
            for d in day_names:
                if d in day_text:
                    current_day = d
                    break
            if not current_day:
                # Если день не распознан, возможно, это строка продолжения без rowspan?
                # Пробуем использовать предыдущий день
                if current_day is None:
                    continue
                # Если current_day уже есть, считаем что это продолжение без rowspan
                time_idx = 0
                lesson_idx = target_col - 1 if target_col > 0 else 0
                rowspan_rem = 0
            else:
                rowspan_rem = int(day_cell.get('rowspan', 1))
                time_idx = 1
                lesson_idx = target_col
        else:
            rowspan_rem -= 1
            time_idx = 0
            lesson_idx = target_col - 1 if target_col > 0 else 0

        if time_idx >= len(cells) or lesson_idx >= len(cells):
            continue

        time_cell = cells[time_idx]
        time_str = time_cell.get_text(strip=True)
        if not re.match(r'^\d{1,2}\.\d{2}$', time_str):
            continue

        lesson_cell = cells[lesson_idx]
        lesson_text = lesson_cell.get_text(strip=True)
        if not lesson_text or lesson_text == '-':
            continue
        if lesson_text == time_str:
            continue

        subject, teacher, room = parse_lesson_text(lesson_text)
        if not subject:
            subject = lesson_text

        day_key = day_short.get(current_day, current_day[:2])
        schedule.setdefault(day_key, []).append({
            'time': time_str,
            'subject': subject,
            'teacher': teacher,
            'room': room
        })

    # Удаление дубликатов
    for day in schedule:
        unique = []
        seen = set()
        for item in schedule[day]:
            key = (item['time'], item['subject'])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        schedule[day] = sorted(unique, key=lambda x: x['time'])

    if not schedule:
        raise ValueError(f"{source}: расписание пустое")
    return schedule

def parse_lesson_text(text):
    if not text:
        return None, None, None
    text = re.sub(r'\s+', ' ', text.strip())
    room = None
    m = re.search(r'\(([^)]+)\)$', text)
    if m:
        room = m.group(1).strip()
        text = text[:m.start()].strip()
    teacher = None
    match = re.search(r'(?:доц\.|ст\.пр\.|проф\.|преп\.)?\s*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.[А-ЯЁ]\.)', text, re.I)
    if match:
        teacher = match.group(0).strip()
        text = text[:match.start()].strip()
    return text or "Без названия", teacher, room

# ---------- Чётность недели ----------
def get_week_parity(date):
    delta = date - SEMESTER_START.date()
    week_number = delta.days // 7 + 1
    return "нечётная" if week_number % 2 == 1 else "чётная"

# ---------- Форматирование ----------
def format_schedule_for_day(target_date):
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        logging.exception("Ошибка загрузки")
        return f"❌ Ошибка загрузки расписания: {e}"

    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    day_name = weekdays[target_date.weekday()]
    if day_name == 'Вс':
        return "📅 В воскресенье пар нет."

    lessons = schedule.get(day_name, [])
    parity = get_week_parity(target_date)
    header = f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\n{parity} неделя\n"

    if not lessons:
        return header + "\n🌟 Нет занятий 🌟!"

    lines = [header]
    for l in lessons:
        room_str = f" в {l['room']}" if l.get('room') else ""
        teacher_str = f"\n👨‍🏫 {l['teacher']}" if l.get('teacher') else ""
        lines.append(f"🕒 {l['time']}:\n📚 {l['subject']}{room_str}{teacher_str}\n")
    return "\n".join(lines)

def format_full_week():
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка: {e}"

    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб']
    result = "📆 Расписание на неделю:\n\n"
    for i, day in enumerate(weekdays):
        day_date = start + timedelta(days=i)
        lessons = schedule.get(day, [])
        parity = get_week_parity(day_date)
        result += f"◾◼🔲📃{day} {day_date.day}📄🔳◻◽\n{parity} неделя\n"
        if not lessons:
            result += "🌟 Нет занятий 🌟!\n\n"
        else:
            for l in lessons:
                room_str = f" в {l['room']}" if l.get('room') else ""
                teacher_str = f"\n👨‍🏫 {l['teacher']}" if l.get('teacher') else ""
                result += f"🕒 {l['time']}:\n📚 {l['subject']}{room_str}{teacher_str}\n"
            result += "\n"
    return result

# ---------- Telegram Bot ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        f"👋 Привет! Я бот расписания для группы {GROUP_VARIANTS[0]}.\n"
        f"Версия: {VERSION}\n\n"
        "Команды:\n"
        "/today – сегодня\n"
        "/tomorrow – завтра\n"
        "/week – вся неделя"
    )

@dp.message(Command("today"))
async def today_cmd(message: types.Message):
    await message.answer(format_schedule_for_day(datetime.now().date()))

@dp.message(Command("tomorrow"))
async def tomorrow_cmd(message: types.Message):
    await message.answer(format_schedule_for_day(datetime.now().date() + timedelta(days=1)))

@dp.message(Command("week"))
async def week_cmd(message: types.Message):
    await message.answer(format_full_week())

# ---------- Веб-сервер ----------
async def handle_health(request):
    return web.Response(text=f"Bot is running. Version: {VERSION}")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logging.info("Web server started on port 8080")

async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        get_cached_schedule()
        logging.info("Расписание успешно загружено при старте")
    except Exception as e:
        logging.error(f"Критическая ошибка при старте: {e}")

    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
