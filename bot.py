import os
import asyncio
import logging
import re
import time
import json
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

API_URL = "https://para.alf-kai.ru/?q=24100"
FALLBACK_URL = "https://alf-kai.ru/расписание/"
GROUP_NAME = "24100 (09.03.03)"
SEMESTER_START = datetime(2026, 2, 9)   # начало семестра (понедельник, нечётная неделя)
VERSION = "2026-04-08-debug"

# ---------- Кэш ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule()
        _cache["expires"] = now + 3600
    return _cache["data"]

# ---------- Универсальная загрузка с диагностикой ----------
def load_schedule():
    """Пробует API, затем fallback HTML, с подробным логированием."""
    # 1. Пробуем API
    try:
        resp = requests.get(API_URL, timeout=10)
        resp.raise_for_status()
        content_type = resp.headers.get('content-type', '')
        logging.info(f"API ответ: статус {resp.status_code}, Content-Type: {content_type}")
        # Логируем первые 500 символов для анализа
        logging.info(f"API фрагмент ответа: {resp.text[:500]}")

        if 'application/json' in content_type:
            data = resp.json()
            return parse_api_json(data)
        else:
            # Возможно, API вернул HTML (редирект или ошибка)
            logging.warning("API вернул не JSON, пробуем распарсить как HTML")
            return parse_html(resp.text)
    except Exception as e:
        logging.error(f"Ошибка API: {e}")

    # 2. Fallback: прямой запрос к alf-kai.ru/расписание/
    try:
        resp = requests.get(FALLBACK_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        resp.raise_for_status()
        logging.info(f"Fallback HTML ответ: статус {resp.status_code}, длина {len(resp.text)}")
        return parse_html(resp.text)
    except Exception as e:
        logging.error(f"Ошибка fallback: {e}")
        raise ValueError("Не удалось загрузить расписание ни через API, ни через HTML")

def parse_api_json(data):
    """Обработка JSON от API (если он всё же есть)."""
    day_map = {
        "Понедельник": "Пн", "Вторник": "Вт", "Среда": "Ср",
        "Четверг": "Чт", "Пятница": "Пт", "Суббота": "Сб", "Воскресенье": "Вс"
    }
    schedule = {}
    for rus_day, lessons in data.items():
        short_day = day_map.get(rus_day, rus_day[:2])
        if not lessons:
            schedule[short_day] = []
            continue
        parsed = []
        for item in lessons:
            time_str = item.get("time", "").strip()
            subject = item.get("subject", "").strip()
            teacher = item.get("teacher", "").strip()
            room = item.get("room", "").strip()
            if subject and subject != "-":
                parsed.append({
                    "time": time_str,
                    "subject": subject,
                    "teacher": teacher or None,
                    "room": room or None
                })
        parsed.sort(key=lambda x: x["time"])
        schedule[short_day] = parsed
    if not schedule:
        raise ValueError("API JSON пуст")
    return schedule

def parse_html(html):
    """Парсинг HTML таблицы с расписанием."""
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.find_all('table')
    logging.info(f"Найдено таблиц: {len(tables)}")
    if not tables:
        raise ValueError("HTML не содержит таблиц")

    # Ищем таблицу, в которой есть ячейки с днями недели
    target_table = None
    for tbl in tables:
        rows = tbl.find_all('tr')
        if not rows:
            continue
        # Проверяем первую строку на наличие дней недели или "24100"
        first_row_text = rows[0].get_text()
        if any(day in first_row_text for day in ['Понедельник', 'Вторник', '24100']):
            target_table = tbl
            break
    if not target_table:
        target_table = tables[0]  # берём первую попавшуюся

    rows = target_table.find_all('tr')
    if len(rows) < 2:
        raise ValueError("В таблице недостаточно строк")

    # Определяем колонку для группы 24100 (ищем в шапке)
    header_cells = rows[0].find_all(['td', 'th'])
    target_col = None
    for idx, cell in enumerate(header_cells):
        cell_text = cell.get_text(strip=True)
        if '24100' in cell_text or '09.03.03' in cell_text:
            target_col = idx
            break
    if target_col is None:
        # Пытаемся найти колонку по умолчанию (обычно третья)
        if len(header_cells) >= 3:
            target_col = 2
            logging.warning(f"Колонка группы не найдена, используется индекс {target_col}")
        else:
            raise ValueError("Не удалось определить колонку группы")

    logging.info(f"Используется колонка {target_col} из {len(header_cells)}")

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
                # Может быть, день в первой ячейке, но сокращённо? Пропускаем строки без дня
                continue
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
        raise ValueError("После парсинга расписание пустое")
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
        f"👋 Привет! Я бот расписания для группы {GROUP_NAME}.\n"
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

# ---------- Веб-сервер (aiohttp) ----------
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

# ---------- Главная ----------
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
