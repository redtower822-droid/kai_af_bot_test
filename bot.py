import os
import asyncio
import logging
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

SCHEDULE_URL = "https://docs.google.com/document/u/0/d/1ZjBfEvJzmluiZy-5HqvulPHHfqjYTbxltDp4hbCdZWc/pub"
GROUP_NAME = "09.03.03"

# ---------- КЭШ ----------
_cache = {
    "data": None,
    "expires": 0
}

def get_cached_schedule():
    """Возвращает расписание из кэша или загружает новое, если кэш устарел (1 час)"""
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600  # 1 час
    return _cache["data"]

def load_schedule_from_google():
    """Загружает и парсит расписание из Google Docs. Возвращает (week_schedules, days_names)"""
    try:
        resp = requests.get(SCHEDULE_URL, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        tables = soup.find_all('table')
        if len(tables) < 2:
            # Возможно, таблица одна и внутри неё две части (чёт/нечет) – пробуем распарсить как две
            # Но для вашего документа точно две таблицы
            raise ValueError(f"Найдено таблиц: {len(tables)}, ожидалось 2")
        
        week_schedules = {}
        week_names = ["even", "odd"]
        days_names = []
        
        for idx, table in enumerate(tables[:2]):
            rows = table.find_all('tr')
            if not rows:
                continue
            
            # Заголовок: ищем строку с днями недели
            header_row = rows[0]
            day_cells = header_row.find_all(['td', 'th'])
            days = []
            for cell in day_cells:
                text = cell.get_text(strip=True)
                # Игнорируем ячейку "Время" или "Time"
                if text and text.lower() not in ("время", "time"):
                    days.append(text)
            
            if idx == 0:
                days_names = days  # сохраняем названия дней из первой таблицы
            
            schedule = {i: {} for i in range(len(days))}
            for row in rows[1:]:
                cols = row.find_all('td')
                if len(cols) < len(days) + 1:
                    continue
                time_slot = cols[0].get_text(strip=True)
                if not time_slot:
                    continue
                for day_idx in range(len(days)):
                    subject = cols[day_idx + 1].get_text(strip=True)
                    if subject and subject != "-":
                        schedule[day_idx][time_slot] = subject
            week_schedules[week_names[idx]] = schedule
        
        return week_schedules, days_names
    except Exception as e:
        logging.error(f"Ошибка парсинга расписания: {e}")
        raise

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def get_week_type():
    """Определяет чётность недели. Начало учебного года: 1 сентября 2025 (нечётная)"""
    start_date = datetime(2025, 9, 1).date()
    today = datetime.now().date()
    delta_days = (today - start_date).days
    if delta_days < 0:
        return "odd"  # нечётная
    week_num = delta_days // 7
    return "odd" if week_num % 2 == 0 else "even"

def get_day_index(weekday):
    """weekday: 0=пн ... 5=сб"""
    mapping = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб"}
    return mapping.get(weekday)

def format_schedule_for_day(target_date):
    """Возвращает отформатированное расписание на указанную дату"""
    try:
        week_schedules, days_names = get_cached_schedule()
    except Exception:
        return "❌ Не удалось загрузить расписание. Попробуйте позже."
    
    weekday_num = target_date.weekday()
    if weekday_num > 5:  # воскресенье
        return "📅 В воскресенье пар нет."
    
    day_name = get_day_index(weekday_num)
    if day_name not in days_names:
        return "❌ Расписание на этот день не найдено."
    
    day_idx = days_names.index(day_name)
    week_type = get_week_type()
    schedule_day = week_schedules[week_type].get(day_idx, {})
    
    if not schedule_day:
        week_rus = "чётной" if week_type == "even" else "нечётной"
        return f"📭 На {day_name} ({week_rus} неделе) пар нет."
    
    week_rus = "чётная" if week_type == "even" else "нечётная"
    lines = [f"📅 {target_date.strftime('%d.%m.%Y')} ({day_name}), {week_rus} неделя\n"]
    for time_slot in sorted(schedule_day.keys()):
        subject = schedule_day[time_slot]
        lines.append(f"🕒 {time_slot} — {subject}")
    return "\n".join(lines)

def format_full_week():
    """Возвращает расписание на всю текущую неделю"""
    try:
        week_schedules, days_names = get_cached_schedule()
    except Exception:
        return "❌ Не удалось загрузить расписание."
    
    week_type = get_week_type()
    week_schedule = week_schedules[week_type]
    today = datetime.now().date()
    start_of_week = today - timedelta(days=today.weekday())
    week_rus = "чётную" if week_type == "even" else "нечётную"
    lines = [f"📆 Расписание на {week_rus} неделю:\n"]
    
    for i, day_name in enumerate(days_names):
        day_date = start_of_week + timedelta(days=i)
        lessons = week_schedule.get(i, {})
        if lessons:
            lines.append(f"*{day_name} ({day_date.strftime('%d.%m')})*")
            for t, subj in lessons.items():
                lines.append(f"  {t} — {subj}")
            lines.append("")
        else:
            lines.append(f"*{day_name}* — пар нет\n")
    return "\n".join(lines)

# ---------- БОТ ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        f"👋 Привет! Я бот расписания для группы {GROUP_NAME}.\n"
        "Команды:\n"
        "/today — пары на сегодня\n"
        "/tomorrow — пары на завтра\n"
        "/week — расписание на текущую неделю\n"
        "Расписание берётся из официального Google Docs."
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
    await message.answer(text, parse_mode="Markdown")

async def main():
    logging.basicConfig(level=logging.INFO)
    # Загружаем расписание при старте, чтобы кэш заполнился
    try:
        get_cached_schedule()
        logging.info("Расписание успешно загружено при старте")
    except Exception as e:
        logging.error(f"Не удалось загрузить расписание при старте: {e}")
    await dp.start_polling(bot)

# ---------- ВЕБ-СЕРВЕР ДЛЯ ПИНГОВ (чтобы бот не засыпал) ----------
from flask import Flask
from threading import Thread

flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Бот работает!"

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    keep_alive()   # запускаем веб-сервер в фоне
    asyncio.run(main())