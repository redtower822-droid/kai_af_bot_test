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
VERSION = "2026-04-06-v5-fixed"

# ---------- КЭШ ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600
    return _cache["data"]

def clean_text(t):
    return re.sub(r'\s+', ' ', t).strip()

def parse_lesson(text):
    """Извлекает предмет, преподавателя, аудиторию из текста ячейки"""
    if not text or text == '-':
        return None, None, None
    # Аудитория в скобках в конце
    room = None
    m = re.search(r'\(([^)]+)\)$', text)
    if m:
        room = m.group(1)
        text = text[:m.start()].strip()
    # Преподаватель: обычно после должности или в конце с инициалами
    teacher = None
    # Паттерн: должность (доц., ст.пр., проф.) + Фамилия И.О. или просто Фамилия И.О.
    match = re.search(r'(доц\.|ст\.пр\.|проф\.|преп\.)\s+([А-Я][а-я]+\s+[А-Я]\.[А-Я]\.?)', text)
    if not match:
        match = re.search(r'([А-Я][а-я]+\s+[А-Я]\.[А-Я]\.?)', text)
    if match:
        teacher = match.group(0)
        text = text[:match.start()].strip()
    return text, teacher, room

def load_schedule_from_google():
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(SCHEDULE_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Находим таблицу
    table = soup.find('table', class_='c19')
    if not table:
        table = soup.find('table')
    if not table:
        raise ValueError("Таблица не найдена")
    
    rows = table.find_all('tr')
    # Определяем индекс колонки для группы 24100 (09.03.03)
    # Заголовок в первой строке (row 0)
    header_row = rows[0]
    headers = [clean_text(cell.get_text()) for cell in header_row.find_all(['td', 'th'])]
    target_col = None
    for i, h in enumerate(headers):
        if '24100' in h or '09.03.03' in h:
            target_col = i
            break
    if target_col is None:
        # По умолчанию вторая колонка (индекс 1)
        target_col = 1
    
    # Маппинг дней недели
    day_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    day_short = {'Понедельник': 'Пн', 'Вторник': 'Вт', 'Среда': 'Ср', 'Четверг': 'Чт', 'Пятница': 'Пт', 'Суббота': 'Сб'}
    
    schedule = {}
    current_day = None
    rowspan = 0
    
    # Пропускаем первую строку (заголовок)
    for row in rows[1:]:
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue
        
        # Ячейка дня (первая колонка)
        if rowspan == 0:
            day_cell = cells[0]
            day_text = clean_text(day_cell.get_text())
            # Извлекаем название дня без даты
            for d in day_names:
                if d in day_text:
                    current_day = d
                    break
            if not current_day:
                # Если не нашли, берём первое слово
                current_day = day_text.split()[0]
            rowspan = int(day_cell.get('rowspan', 1))
            start_col = 1
        else:
            rowspan -= 1
            start_col = 0
        
        if not current_day:
            continue
        
        # Теперь ищем время и предмет
        # Время обычно в первой ячейке после дня (индекс start_col)
        # Но в некоторых строках время может быть не в первой, а во второй? Нет, в таблице время всегда сразу после дня.
        # Однако из-за rowspan ячейка дня может отсутствовать, тогда время будет в первой ячейке (индекс 0).
        time_cell = None
        lesson_cell = None
        # Ищем ячейку с временем (цифры с точкой или двоеточием)
        for i in range(start_col, len(cells)):
            cell_text = clean_text(cells[i].get_text())
            if re.match(r'^\d{1,2}[\.:]\d{2}$', cell_text):
                time_cell = cells[i]
                # Предмет для нашей группы находится на позиции target_col относительно начала строки?
                # В каждой строке порядок колонок: день (если есть), время, предмет_24100, время_24200, предмет_24200, ...
                # Поэтому предмет для 24100 всегда идёт сразу после времени? Нет, после времени идёт предмет для 24100, затем время для 24200 и т.д.
                # Но из-за того, что target_col может быть больше 1, нужно брать ячейку с индексом target_col, если она есть.
                if target_col < len(cells):
                    lesson_cell = cells[target_col]
                break
        if not time_cell or not lesson_cell:
            continue
        
        time_str = clean_text(time_cell.get_text())
        lesson_text = clean_text(lesson_cell.get_text())
        if not lesson_text or lesson_text == '-':
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
    
    if not schedule:
        raise ValueError("Не удалось извлечь расписание. Возможно, изменилась структура таблицы.")
    
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
        logging.info("Расписание загружено успешно")
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
