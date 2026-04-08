import os
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

API_URL = "https://para.alf-kai.ru/?q=24100"           # основной источник
FALLBACK_URL = "https://alf-kai.ru/расписание/#old_rasp"  # если API упадёт (парсим HTML)
GROUP_NAME = "24100 (09.03.03)"
SEMESTER_START = datetime(2026, 2, 9)   # начало семестра (понедельник, нечётная неделя)
VERSION = "2026-04-08-alf-api"

# ---------- Кэш ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_api()
        _cache["expires"] = now + 3600
    return _cache["data"]

# ---------- Загрузка через API ----------
def load_schedule_from_api():
    """Получает расписание с para.alf-kai.ru в формате JSON."""
    import requests
    try:
        resp = requests.get(API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.error(f"Ошибка API: {e}, пробуем парсинг HTML")
        return load_schedule_from_html_fallback()

    # Маппинг русских дней в краткие обозначения
    day_map = {
        "Понедельник": "Пн",
        "Вторник": "Вт",
        "Среда": "Ср",
        "Четверг": "Чт",
        "Пятница": "Пт",
        "Суббота": "Сб",
        "Воскресенье": "Вс"
    }
    schedule = {}
    for rus_day, lessons in data.items():
        short_day = day_map.get(rus_day, rus_day[:2])
        if not lessons:
            schedule[short_day] = []
            continue
        parsed = []
        for item in lessons:
            # Приводим к единому формату
            time_str = item.get("time", "").strip()
            subject = item.get("subject", "").strip()
            teacher = item.get("teacher", "").strip()
            room = item.get("room", "").strip()

            if not subject or subject == "-":
                continue

            parsed.append({
                "time": time_str,
                "subject": subject,
                "teacher": teacher if teacher else None,
                "room": room if room else None
            })
        # Сортируем по времени
        parsed.sort(key=lambda x: x["time"])
        schedule[short_day] = parsed

    # Логирование
    for day in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]:
        cnt = len(schedule.get(day, []))
        logging.info(f"{day}: {cnt} пар")
    if not schedule:
        raise ValueError("API вернул пустое расписание")
    return schedule

# ---------- Fallback: парсинг HTML с alf-kai.ru (если API недоступен) ----------
def load_schedule_from_html_fallback():
    import requests
    from bs4 import BeautifulSoup

    url = "https://alf-kai.ru/расписание/"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Ищем таблицу с расписанием (обычно это первая большая таблица)
    table = soup.find("table")
    if not table:
        raise ValueError("HTML fallback: таблица не найдена")

    rows = table.find_all("tr")
    if len(rows) < 2:
        raise ValueError("Слишком мало строк в таблице")

    # Ищем колонку для группы 24100
    header_cells = rows[0].find_all(["td", "th"])
    target_col = None
    for idx, cell in enumerate(header_cells):
        if "24100" in cell.get_text() or "09.03.03" in cell.get_text():
            target_col = idx
            break
    if target_col is None:
        target_col = 2
        logging.warning("Колонка группы не найдена, fallback index=2")

    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
    day_short = {"Понедельник": "Пн", "Вторник": "Вт", "Среда": "Ср",
                 "Четверг": "Чт", "Пятница": "Пт", "Суббота": "Сб"}

    schedule = {}
    current_day = None
    rowspan_rem = 0

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        if rowspan_rem == 0:
            day_cell = cells[0]
            day_text = day_cell.get_text(strip=True)
            for d in day_names:
                if d in day_text:
                    current_day = d
                    break
            if not current_day:
                continue
            rowspan_rem = int(day_cell.get("rowspan", 1))
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
        if not re.match(r"^\d{1,2}\.\d{2}$", time_str):
            continue

        lesson_cell = cells[lesson_idx]
        lesson_text = lesson_cell.get_text(strip=True)
        if not lesson_text or lesson_text == "-":
            continue

        # Парсим занятие (упрощённо)
        subject, teacher, room = parse_lesson_text(lesson_text)
        if not subject:
            subject = lesson_text

        day_key = day_short.get(current_day, current_day[:2])
        schedule.setdefault(day_key, []).append({
            "time": time_str,
            "subject": subject,
            "teacher": teacher,
            "room": room
        })

    # Удаление дубликатов
    for day in schedule:
        unique = []
        seen = set()
        for item in schedule[day]:
            key = (item["time"], item["subject"])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        schedule[day] = sorted(unique, key=lambda x: x["time"])

    if not schedule:
        raise ValueError("HTML fallback: расписание пустое")
    return schedule

def parse_lesson_text(text):
    """Вспомогательная функция для fallback-парсинга."""
    if not text:
        return None, None, None
    text = re.sub(r"\s+", " ", text.strip())
    room = None
    m = re.search(r"\(([^)]+)\)$", text)
    if m:
        room = m.group(1).strip()
        text = text[:m.start()].strip()
    teacher = None
    match = re.search(r"(?:доц\.|ст\.пр\.|проф\.|преп\.)?\s*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.[А-ЯЁ]\.)", text, re.I)
    if match:
        teacher = match.group(0).strip()
        text = text[:match.start()].strip()
    return text or "Без названия", teacher, room

# ---------- Чётность недели ----------
def get_week_parity(date: datetime.date) -> str:
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

    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    day_name = weekdays[target_date.weekday()]
    if day_name == "Вс":
        return "📅 В воскресенье пар нет."

    lessons = schedule.get(day_name, [])
    parity = get_week_parity(target_date)
    header = f"◾◼🔲📃{day_name} {target_date.day}📄🔳◻◽\n{parity} неделя\n"

    if not lessons:
        return header + "\n🌟 Нет занятий 🌟!"

    lines = [header]
    for l in lessons:
        room_str = f" в {l['room']}" if l.get("room") else ""
        teacher_str = f"\n👨‍🏫 {l['teacher']}" if l.get("teacher") else ""
        lines.append(f"🕒 {l['time']}:\n📚 {l['subject']}{room_str}{teacher_str}\n")
    return "\n".join(lines)

def format_full_week():
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        return f"❌ Ошибка: {e}"

    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
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
                room_str = f" в {l['room']}" if l.get("room") else ""
                teacher_str = f"\n👨‍🏫 {l['teacher']}" if l.get("teacher") else ""
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
