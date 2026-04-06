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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

SCHEDULE_URL = "https://docs.google.com/document/u/0/d/1ZjBfEvJzmluiZy-5HqvulPHHfqjYTbxltDp4hbCdZWc/pub"
GROUP_NAME = "09.03.03"
VERSION = "2026-04-06-v14"

_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600
    return _cache["data"]

def parse_lesson(text):
    """Извлекает предмет, преподавателя, аудиторию."""
    if not text or text == '-':
        return None, None, None
    # Аудитория в скобках
    room = None
    m = re.search(r'\(([^)]+)\)$', text)
    if m:
        room = m.group(1)
        text = text[:m.start()].strip()
    # Преподаватель
    teacher = None
    match = re.search(r'(доц\.|ст\.пр\.|проф\.|преп\.)\s+([А-Я][а-я]+\s+[А-Я]\.[А-Я]\.?)', text)
    if not match:
        match = re.search(r'([А-Я][а-я]+\s+[А-Я]\.[А-Я]\.?)', text)
    if match:
        teacher = match.group(0)
        text = text[:match.start()].strip()
    text = re.sub(r'\s+', ' ', text).strip()
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
    # Определяем колонку для группы 24100 (09.03.03)
    header_row = rows[0]
    header_cells = header_row.find_all(['td', 'th'])
    target_col = None
    for idx, cell in enumerate(header_cells):
        if '24100' in cell.get_text():
            target_col = idx
            break
    if target_col is None:
        target_col = 2  # по умолчанию третья колонка (индекс 2)
    logging.info(f"Колонка для 24100: {target_col}")

    # Маппинг дней
    day_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    day_short = {'Понедельник': 'Пн', 'Вторник': 'Вт', 'Среда': 'Ср', 'Четверг': 'Чт', 'Пятница': 'Пт', 'Суббота': 'Сб'}
    schedule = {}
    current_day = None
    rowspan = 0

    for row in rows[1:]:
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue
        # Определяем, есть ли в строке день
        if rowspan == 0:
            # Новая строка с днём
            day_cell = cells[0]
            day_text = day_cell.get_text(strip=True)
            for d in day_names:
                if d in day_text:
                    current_day = d
                    break
            if not current_day:
                current_day = day_text.split()[0]
            rowspan = int(day_cell.get('rowspan', 1))
            # В такой строке индекс времени = 1
            time_idx = 1
            # Индекс предмета = target_col
            lesson_idx = target_col
        else:
            # Продолжение предыдущего дня
            rowspan -= 1
            time_idx = 0
            # В строках без дня колонка дня отсутствует, поэтому предмет смещается на 1 влево
            lesson_idx = target_col - 1 if target_col > 0 else 0

        if not current_day:
            continue

        # Проверяем наличие ячеек
        if time_idx >= len(cells) or lesson_idx >= len(cells):
            continue

        time_cell = cells[time_idx]
        time_str = time_cell.get_text(strip=True)
        # Проверка формата времени
        if not re.match(r'^\d{1,2}\.\d{2}$', time_str):
            continue

        lesson_cell = cells[lesson_idx]
        lesson_text = lesson_cell.get_text(strip=True)
        if not lesson_text or lesson_text == '-':
            continue

        # Пропускаем строки, где предмет совпадает со временем (например "8.15")
        if lesson_text == time_str:
            continue

        subject, teacher, room = parse_lesson(lesson_text)
        if not subject:
            subject = lesson_text

        day_key = day_short.get(current_day, current_day[:2])
        schedule.setdefault(day_key, []).append({
            'time': time_str,
            'subject': subject,
            'teacher': teacher,
            'room': room
        })

    # Удаляем дубликаты (если одинаковое время и предмет)
    for day in schedule:
        unique = []
        seen = set()
        for item in schedule[day]:
            key = (item['time'], item['subject'])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        schedule[day] = unique

    # Логируем результат
    for day in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб']:
        count = len(schedule.get(day, []))
        logging.info(f"{day}: {count} пар")
    if not schedule:
        raise ValueError("Расписание не извлечено")
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
        logging.error(f"Ошибка: {e}")
    await dp.start_polling(bot)

flask_app = Flask('')
@flask_app.route('/')
def home():
    return f"Бот работает. Версия: {VERSION}"

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
