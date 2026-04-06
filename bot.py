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

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

SCHEDULE_URL = "https://docs.google.com/document/u/0/d/1ZjBfEvJzmluiZy-5HqvulPHHfqjYTbxltDp4hbCdZWc/pub"
GROUP_NAME = "09.03.03"

# ---------- КЭШ ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600  # 1 час
    return _cache["data"]

def parse_lesson_text(text):
    """Извлекает из текста ячейки: предмет, аудиторию, преподавателя"""
    # Пример: "Математика практика доц. Батурина Р.В. (206)"
    # Или: "Физическая культура и спорт (элективная дисциплина) практика ст.пр. Чукашов А.Н. (фитнес зал)"
    if not text or text == "-":
        return None, None, None
    # Пробуем найти аудиторию в скобках
    room = None
    match_room = re.search(r'\(([^)]+)\)$', text)
    if match_room:
        room = match_room.group(1)
        text = text[:match_room.start()].strip()
    # Преподаватель – обычно после должности (доц., ст.пр., проф.)
    teacher = None
    # Ищем последнюю часть после пробела, которая начинается с заглавной буквы и содержит точку
    parts = text.split()
    for i, part in enumerate(parts):
        if re.match(r'[А-Я][а-я]*\.', part) and i < len(parts)-1:
            # Скорее всего, преподаватель: "доц. Батурина Р.В."
            teacher = " ".join(parts[i:])
            text = " ".join(parts[:i]).strip()
            break
    # Если не нашли, возможно преподаватель без должности
    if not teacher and len(parts) > 1 and re.match(r'[А-Я][а-я]+\s+[А-Я]\.[А-Я]\.', parts[-1]):
        teacher = parts[-1]
        text = " ".join(parts[:-1])
    return text, teacher, room

def load_schedule_from_google():
    """Парсит таблицу с расписанием для группы 24100 (09.03.03)"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(SCHEDULE_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Находим таблицу (первую и главную)
    table = soup.find('table', class_='c19')
    if not table:
        raise ValueError("Не найдена таблица с расписанием")
    
    rows = table.find_all('tr')
    if len(rows) < 3:
        raise ValueError("Таблица слишком мала")
    
    # Определяем индексы колонок для группы 24100 (09.03.03)
    # Заголовок: первая строка (индекс 0) – "Дни недели", "гр. 24100 направление 09.03.03", "гр. 24200 ...", "гр. 24300 ..."
    header_cells = rows[0].find_all(['td', 'th'])
    target_col = -1
    for idx, cell in enumerate(header_cells):
        if '24100' in cell.get_text() or '09.03.03' in cell.get_text():
            target_col = idx
            break
    if target_col == -1:
        # Если не нашли, предположим что вторая колонка (индекс 1)
        target_col = 1
    
    schedule = {}
    current_day = None
    current_day_rowspan = 0
    
    for row in rows[1:]:  # пропускаем заголовок
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue
        
        col_idx = 0
        # Первая ячейка – день недели (может быть с rowspan)
        if current_day_rowspan == 0:
            day_cell = cells[0]
            current_day = day_cell.get_text(strip=True)
            # Извлекаем rowspan
            rowspan = day_cell.get('rowspan')
            current_day_rowspan = int(rowspan) if rowspan and rowspan.isdigit() else 1
            # Убираем число (дату) из названия дня, если есть
            current_day = re.sub(r'\s+\d+', '', current_day).strip()
            col_idx = 1
        else:
            current_day_rowspan -= 1
            col_idx = 0
        
        # Если день не распознан – пропускаем
        if not current_day:
            continue
        
        # В нужной колонке ищем время и предмет
        # Время обычно во второй ячейке (индекс 1), но с учётом сдвига из-за rowspan
        # Упростим: в каждой строке есть ячейка с временем (содержит цифры и двоеточие)
        time_cell = None
        lesson_cell = None
        for i in range(len(cells)):
            text = cells[i].get_text(strip=True)
            if re.match(r'^\d{1,2}\.\d{2}$', text) or re.match(r'^\d{1,2}:\d{2}$', text):
                time_cell = cells[i]
                # Предмет находится в следующей ячейке после времени? Не всегда.
                # В таблице порядок: день, время, предмет_24100, время_24200, предмет_24200, ...
                # Поэтому если мы нашли время, то предмет для 24100 будет через одну ячейку?
                # Лучше искать по индексу target_col.
                break
        if not time_cell:
            continue
        # Находим предмет для нашей группы: это ячейка с индексом target_col
        if target_col < len(cells):
            lesson_cell = cells[target_col]
        else:
            continue
        
        time_str = time_cell.get_text(strip=True)
        lesson_text = lesson_cell.get_text(strip=True)
        if not lesson_text or lesson_text == '-':
            continue
        
        subject, teacher, room = parse_lesson_text(lesson_text)
        if not subject:
            subject = lesson_text
        
        # Сохраняем пары для дня
        if current_day not in schedule:
            schedule[current_day] = []
        schedule[current_day].append({
            'time': time_str,
            'subject': subject,
            'teacher': teacher,
            'room': room
        })
    
    # Нормализуем названия дней (на случай если пришли с датой)
    day_mapping = {
        'Понедельник': 'Пн',
        'Вторник': 'Вт',
        'Среда': 'Ср',
        'Четверг': 'Чт',
        'Пятница': 'Пт',
        'Суббота': 'Сб'
    }
    normalized = {}
    for day, lessons in schedule.items():
        short = day_mapping.get(day, day[:2])
        normalized[short] = lessons
    return normalized

def get_week_type():
    # Для нечётной недели всегда возвращаем "odd" (только один документ)
    # Если понадобится чётная – позже добавим вторую ссылку
    return "odd"

def format_schedule_for_day(target_date):
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка загрузки расписания: {e}"
    
    weekday_num = target_date.weekday()  # 0=пн
    weekdays_ru = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    day_name = weekdays_ru[weekday_num]
    if day_name == 'Вс':
        return "📅 В воскресенье пар нет."
    
    lessons = schedule.get(day_name, [])
    if not lessons:
        return f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\n\n🌟Нет занятий🌟!"
    
    week_rus = "нечётная"
    header = f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\n{week_rus} неделя\n"
    lines = [header]
    for lesson in lessons:
        time_str = lesson['time']
        subj = lesson['subject']
        room = f" в {lesson['room']}" if lesson['room'] else ""
        teacher = f"\nПреподаватель: {lesson['teacher']}" if lesson['teacher'] else ""
        lines.append(f"В {time_str}:\n{subj}{room}{teacher}\n")
    return "\n".join(lines)

def format_full_week():
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка загрузки расписания: {e}"
    
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    week_rus = "нечётная"
    result = f"📆 Расписание на {week_rus} неделю:\n\n"
    weekdays_ru = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб']
    for i, day_name in enumerate(weekdays_ru):
        day_date = start_of_week + timedelta(days=i)
        lessons = schedule.get(day_name, [])
        result += f"◾◼🔲📃{day_name} {day_date.day}📄🔳◻◽\n"
        if not lessons:
            result += "🌟Нет занятий🌟!\n\n"
        else:
            for lesson in lessons:
                room = f" в {lesson['room']}" if lesson['room'] else ""
                teacher = f"\nПреподаватель: {lesson['teacher']}" if lesson['teacher'] else ""
                result += f"В {lesson['time']}:\n{lesson['subject']}{room}{teacher}\n"
            result += "\n"
    return result

# ---------- БОТ ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        f"👋 Привет! Я бот расписания для группы {GROUP_NAME}.\n"
        "Команды:\n/today – сегодня\n/tomorrow – завтра\n/week – вся неделя\n"
        "Расписание (нечётная неделя) из Google Docs."
    )

@dp.message(Command("today"))
async def today_cmd(message: types.Message):
    text = format_schedule_for_day(datetime.now().date())
    await message.answer(text)

@dp.message(Command("tomorrow"))
async def tomorrow_cmd(message: types.Message):
    text = format_schedule_for_day(datetime.now().date() + timedelta(days=1))
    await message.answer(text)

@dp.message(Command("week"))
async def week_cmd(message: types.Message):
    text = format_full_week()
    await message.answer(text)

async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        get_cached_schedule()
        logging.info("Расписание успешно загружено")
    except Exception as e:
        logging.error(f"Ошибка при старте: {e}")
    await dp.start_polling(bot)

# ---------- ВЕБ-СЕРВЕР ДЛЯ ПИНГОВ ----------
from flask import Flask
from threading import Thread

flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Бот работает"

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

if __name__ == "__main__":
    keep_alive()
    asyncio.run(main())
