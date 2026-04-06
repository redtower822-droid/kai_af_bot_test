import os
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from flask import Flask
from threading import Thread

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

SCHEDULE_URL = "https://docs.google.com/document/u/0/d/1ZjBfEvJzmluiZy-5HqvulPHHfqjYTbxltDp4hbCdZWc/pub"
GROUP_NAME = "09.03.03"
VERSION = "2026-04-06-v3"  # авто-версия при каждом моём ответе

# ---------- КЭШ ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600
    return _cache["data"]

def parse_lesson_text(text):
    if not text or text == "-":
        return None, None, None
    room = None
    match_room = re.search(r'\(([^)]+)\)$', text)
    if match_room:
        room = match_room.group(1)
        text = text[:match_room.start()].strip()
    teacher = None
    parts = text.split()
    for i, part in enumerate(parts):
        if re.match(r'[А-Я][а-я]*\.', part) and i < len(parts)-1:
            teacher = " ".join(parts[i:])
            text = " ".join(parts[:i]).strip()
            break
    if not teacher and len(parts) > 1 and re.match(r'[А-Я][а-я]+\s+[А-Я]\.[А-Я]\.', parts[-1]):
        teacher = parts[-1]
        text = " ".join(parts[:-1])
    return text, teacher, room

def load_schedule_from_google():
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(SCHEDULE_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    table = soup.find('table')
    if not table:
        raise ValueError("Таблица не найдена")
    rows = table.find_all('tr')
    if len(rows) < 3:
        raise ValueError("Таблица слишком мала")
    # Определяем колонку для группы 24100 (09.03.03)
    header_cells = rows[0].find_all(['td', 'th'])
    target_col = 1  # по умолчанию вторая колонка
    for idx, cell in enumerate(header_cells):
        if '24100' in cell.get_text() or '09.03.03' in cell.get_text():
            target_col = idx
            break
    schedule = {}
    current_day = None
    rowspan_left = 0
    for row in rows[1:]:
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue
        if rowspan_left == 0:
            day_cell = cells[0]
            current_day = day_cell.get_text(strip=True)
            current_day = re.sub(r'\s+\d+', '', current_day).strip()
            rowspan = day_cell.get('rowspan')
            rowspan_left = int(rowspan) if rowspan and rowspan.isdigit() else 1
            start_col = 1
        else:
            rowspan_left -= 1
            start_col = 0
        # Ищем время и предмет
        time_cell = None
        lesson_cell = None
        for i in range(start_col, len(cells)):
            text = cells[i].get_text(strip=True)
            if re.match(r'^\d{1,2}\.\d{2}$', text) or re.match(r'^\d{1,2}:\d{2}$', text):
                time_cell = cells[i]
                # Предмет для нашей группы — через одну ячейку? Нет, просто берём ячейку с индексом target_col, если она есть
                if target_col < len(cells):
                    lesson_cell = cells[target_col]
                break
        if not time_cell or not lesson_cell:
            continue
        time_str = time_cell.get_text(strip=True)
        lesson_text = lesson_cell.get_text(strip=True)
        if not lesson_text or lesson_text == '-':
            continue
        subject, teacher, room = parse_lesson_text(lesson_text)
        if not subject:
            subject = lesson_text
        day_map = {'Понедельник': 'Пн', 'Вторник': 'Вт', 'Среда': 'Ср', 'Четверг': 'Чт', 'Пятница': 'Пт', 'Суббота': 'Сб'}
        day_short = day_map.get(current_day, current_day[:2])
        schedule.setdefault(day_short, []).append({
            'time': time_str,
            'subject': subject,
            'teacher': teacher,
            'room': room
        })
    return schedule

def format_schedule_for_day(target_date):
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка: {e}"
    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    day_name = weekdays[target_date.weekday()]
    if day_name == 'Вс':
        return "📅 В воскресенье пар нет."
    lessons = schedule.get(day_name, [])
    if not lessons:
        return f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\n\n🌟Нет занятий🌟!"
    header = f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\nнечётная неделя\n"
    lines = [header]
    for l in lessons:
        room = f" в {l['room']}" if l['room'] else ""
        teacher = f"\nПреподаватель: {l['teacher']}" if l['teacher'] else ""
        lines.append(f"В {l['time']}:\n{l['subject']}{room}{teacher}\n")
    return "\n".join(lines)

def format_full_week():
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка: {e}"
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб']
    result = "📆 Расписание на нечётную неделю:\n\n"
    for i, day in enumerate(weekdays):
        day_date = start + timedelta(days=i)
        lessons = schedule.get(day, [])
        result += f"◾◼🔲📃{day} {day_date.day}📄🔳◻◽\n"
        if not lessons:
            result += "🌟Нет занятий🌟!\n\n"
        else:
            for l in lessons:
                room = f" в {l['room']}" if l['room'] else ""
                teacher = f"\nПреподаватель: {l['teacher']}" if l['teacher'] else ""
                result += f"В {l['time']}:\n{l['subject']}{room}{teacher}\n"
            result += "\n"
    return result

# ---------- БОТ ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        f"👋 Привет! Я бот расписания для группы {GROUP_NAME}.\n"
        f"Версия: {VERSION}\n"
        "Команды:\n/today – сегодня\n/tomorrow – завтра\n/week – вся неделя"
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

async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        get_cached_schedule()
        logging.info("Расписание загружено")
    except Exception as e:
        logging.error(f"Ошибка при старте: {e}")
    await dp.start_polling(bot)

# ---------- ВЕБ-СЕРВЕР ----------
flask_app = Flask('')
@flask_app.route('/')
def home():
    return f"Бот работает. Версия: {VERSION}"

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
