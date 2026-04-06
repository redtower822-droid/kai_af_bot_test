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
VERSION = "2026-04-06-v10"

_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600
    return _cache["data"]

def parse_lesson(text):
    if not text or text == '-':
        return None, None, None
    room = None
    m = re.search(r'\(([^)]+)\)$', text)
    if m:
        room = m.group(1)
        text = text[:m.start()].strip()
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
    # Определяем колонку для группы 24100 (индекс заголовка)
    header_row = rows[0]
    header_cells = header_row.find_all(['td', 'th'])
    target_col = None
    for idx, cell in enumerate(header_cells):
        if '24100' in cell.get_text():
            target_col = idx
            break
    if target_col is None:
        target_col = 1  # вторая колонка
    logging.info(f"Колонка группы 24100: {target_col}")

    day_short = {'Понедельник': 'Пн', 'Вторник': 'Вт', 'Среда': 'Ср', 'Четверг': 'Чт', 'Пятница': 'Пт', 'Суббота': 'Сб'}
    schedule = {}
    current_day = None
    rowspan_left = 0

    # Перебираем все строки, начиная со второй (индекс 1)
    for row in rows[1:]:
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue

        # Проверяем, есть ли в этой строке ячейка дня (первая ячейка)
        if rowspan_left == 0:
            # Это строка, которая начинается с дня
            day_cell = cells[0]
            day_text = day_cell.get_text(strip=True)
            # Извлекаем название дня (до числа)
            for d in day_short.keys():
                if d in day_text:
                    current_day = d
                    break
            if not current_day:
                # Если не нашли, берём первое слово
                current_day = day_text.split()[0]
            rowspan = day_cell.get('rowspan')
            rowspan_left = int(rowspan) if rowspan and rowspan.isdigit() else 1
            # В этой строке после дня идут ячейки: время, предмет_24100, время_24200, предмет_24200, ...
            # Индекс времени = 1
            time_idx = 1
        else:
            # Это продолжение предыдущего дня (rowspan ещё активен)
            rowspan_left -= 1
            # В таких строках нет ячейки дня, первая ячейка — время
            time_idx = 0

        if not current_day:
            continue

        # Проверяем, есть ли ячейка с временем по индексу time_idx
        if time_idx >= len(cells):
            continue
        time_cell = cells[time_idx]
        time_str = time_cell.get_text(strip=True)
        if not re.match(r'^\d{1,2}\.\d{2}$', time_str):
            # Не похоже на время — пропускаем
            continue

        # Теперь ищем предмет для нашей группы
        # Предмет находится в колонке target_col (относительно начала строки)
        # Но если target_col < time_idx, то предмет может быть раньше времени? Нет, в таблице порядок: [день], время, предмет_24100, ...
        # Поэтому предмет всегда идёт после времени.
        # Найдём индекс ячейки предмета: если time_idx=1, то предмет = time_idx+1 = 2? Нет, в первой строке дня: день, время, предмет_24100, ...
        # Значит, предмет = time_idx + 1? Но target_col может быть больше, если в строке несколько временных ячеек.
        # Проще: ищем ячейку с индексом target_col. В первой строке дня target_col=2 (т.к. 0-день, 1-время, 2-предмет_24100).
        # В последующих строках (без дня) target_col=1 (0-время, 1-предмет_24100). Поэтому нужно корректировать target_col в зависимости от наличия дня.
        # Сделаем так: если в строке есть день (rowspan_left > 0? нет, мы уже уменьшили), но мы знаем, что если time_idx == 1, то день присутствовал, и target_col должен быть на 1 больше.
        # Упростим: будем искать предмет в ячейке с индексом target_col, но если target_col >= len(cells), то предмета нет.
        # Однако из-за того, что в строках без дня количество ячеек меньше, target_col может быть сдвинут.
        # Лучше: определим, сколько колонок в строке, и возьмём последнюю доступную ячейку, если target_col выходит за пределы.
        lesson_idx = target_col
        if time_idx == 0 and target_col == 2:
            # В строках без дня первая ячейка — время, вторая — предмет_24100 (индекс 1)
            lesson_idx = 1
        elif time_idx == 1 and target_col == 2:
            lesson_idx = 2
        else:
            lesson_idx = target_col

        if lesson_idx >= len(cells):
            continue
        lesson_cell = cells[lesson_idx]
        lesson_text = lesson_cell.get_text(strip=True)
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

    # Удаляем дубликаты
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
    for day, lessons in schedule.items():
        logging.info(f"День {day}: {len(lessons)} пар")
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
