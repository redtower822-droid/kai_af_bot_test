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
        _cache["expires"] = now + 3600
    return _cache["data"]

def parse_lesson(text):
    """Извлекает из текста ячейки: предмет, тип, аудиторию, преподавателя"""
    # Пример: "Математика лекция в 203\nПреподаватель: Батурина Р.В."
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return None, None, None
    subject = lines[0]
    # Пытаемся найти аудиторию (цифры после "в")
    room = None
    lesson_type = None
    match_room = re.search(r'\bв\s+(\d{3})\b', subject)
    if match_room:
        room = match_room.group(1)
        subject = subject.replace(match_room.group(0), '').strip()
    # Тип занятия (лекция/практика/лаб.раб. и т.д.)
    for t in ['лекция', 'практика', 'лаб.раб.', 'элективная', 'экзамен']:
        if t in subject.lower():
            lesson_type = t
            break
    # Преподаватель
    teacher = None
    for line in lines[1:]:
        if 'преподаватель' in line.lower():
            teacher = line.split(':', 1)[-1].strip()
            break
    return subject, lesson_type, room, teacher

def load_schedule_from_google():
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(SCHEDULE_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    tables = soup.find_all('table')
    if len(tables) < 2:
        raise ValueError(f"Найдено таблиц: {len(tables)}")

    week_schedules = {}
    week_names = ["even", "odd"]  # чётная, нечётная
    days_names = []

    for idx, table in enumerate(tables[:2]):
        rows = table.find_all('tr')
        if not rows:
            continue
        # Заголовок дней недели
        header = rows[0].find_all(['td', 'th'])
        days = []
        for cell in header:
            text = cell.get_text(strip=True)
            if text and text.lower() not in ('время', 'time'):
                days.append(text)
        if idx == 0:
            days_names = days

        schedule = {i: [] for i in range(len(days))}
        for row in rows[1:]:
            cols = row.find_all('td')
            if len(cols) < len(days) + 1:
                continue
            time_slot = cols[0].get_text(strip=True)
            if not time_slot:
                continue
            for day_idx in range(len(days)):
                cell_text = cols[day_idx + 1].get_text(strip=True)
                if cell_text and cell_text != '-':
                    subject, ltype, room, teacher = parse_lesson(cell_text)
                    schedule[day_idx].append({
                        'time': time_slot,
                        'subject': subject,
                        'type': ltype,
                        'room': room,
                        'teacher': teacher
                    })
        week_schedules[week_names[idx]] = schedule

    return week_schedules, days_names

def get_week_type():
    # Начало учебного года 1 сентября 2025 (нечётная)
    start = datetime(2025, 9, 1).date()
    today = datetime.now().date()
    delta = (today - start).days
    if delta < 0:
        return "odd"
    week_num = delta // 7
    return "odd" if week_num % 2 == 0 else "even"

def format_schedule_for_day(target_date):
    try:
        week_schedules, days_names = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка загрузки: {e}"

    weekday = target_date.weekday()  # 0=пн
    if weekday > 5:
        return "📅 В воскресенье пар нет."
    day_name = days_names[weekday] if weekday < len(days_names) else None
    if not day_name:
        return "❌ День не найден"

    week_type = get_week_type()
    lessons = week_schedules[week_type].get(weekday, [])
    if not lessons:
        return f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\n\n🌟Нет занятий🌟!"

    # Красивый вывод
    week_rus = "чётная" if week_type == "even" else "нечётная"
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
        week_schedules, days_names = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка загрузки: {e}"

    week_type = get_week_type()
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    week_rus = "чётная" if week_type == "even" else "нечётная"
    result = f"📆 Расписание на {week_rus} неделю:\n\n"
    for i, day_name in enumerate(days_names):
        day_date = start_of_week + timedelta(days=i)
        lessons = week_schedules[week_type].get(i, [])
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
        f"👋 Привет! Я бот расписания группы {GROUP_NAME}.\n"
        "Команды:\n/today – сегодня\n/tomorrow – завтра\n/week – вся неделя"
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
        logging.info("Расписание загружено")
    except Exception as e:
        logging.error(f"Ошибка при старте: {e}")
    await dp.start_polling(bot)

# ---------- ВЕБ-СЕРВЕР ДЛЯ ПИНГОВ (чтобы не засыпал) ----------
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
