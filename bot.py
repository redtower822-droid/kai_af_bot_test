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

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

SCHEDULE_URL = "https://docs.google.com/document/u/0/d/1ZjBfEvJzmluiZy-5HqvulPHHfqjYTbxltDp4hbCdZWc/pub"
GROUP_VARIANTS = ["24100", "09.03.03"]   # возможные варианты написания группы в шапке
SEMESTER_START = datetime(2026, 2, 9)    # начало семестра (понедельник, нечётная неделя)
VERSION = "2026-04-08-fixed"

# ---------- Кэш ----------
_cache = {"data": None, "expires": 0}

def get_cached_schedule():
    now = time.time()
    if _cache["data"] is None or now > _cache["expires"]:
        _cache["data"] = load_schedule_from_google()
        _cache["expires"] = now + 3600
    return _cache["data"]

# ---------- Парсинг строки занятия ----------
def parse_lesson(text):
    """Извлекает предмет, преподавателя и аудиторию."""
    if not text or text.strip() == '-':
        return None, None, None

    # Очищаем от лишних пробелов
    text = re.sub(r'\s+', ' ', text.strip())

    # Аудитория в скобках (может быть в конце)
    room = None
    m = re.search(r'\(([^)]+)\)$', text)
    if m:
        room = m.group(1).strip()
        text = text[:m.start()].strip()

    # Преподаватель: звание (опционально) + Фамилия И.О.
    teacher_pattern = r'(?:доц\.|ст\.пр\.|проф\.|преп\.|асс\.)?\s*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.[А-ЯЁ]\.)'
    match = re.search(teacher_pattern, text, re.IGNORECASE)
    if match:
        teacher = match.group(0).strip()
        # Убираем преподавателя из текста предмета
        text = text[:match.start()].strip()
    else:
        teacher = None

    # Оставшееся — предмет (может содержать номер пары или доп. инфу, оставляем как есть)
    subject = text or "Без названия"
    return subject, teacher, room

# ---------- Загрузка расписания из Google Docs ----------
def load_schedule_from_google():
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(SCHEDULE_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    table = soup.find('table')
    if not table:
        raise ValueError("Таблица не найдена")

    rows = table.find_all('tr')
    if len(rows) < 2:
        raise ValueError("Слишком мало строк в таблице")

    # Ищем целевую колонку по вариантам группы
    header_row = rows[0]
    header_cells = header_row.find_all(['td', 'th'])
    target_col = None
    for idx, cell in enumerate(header_cells):
        cell_text = cell.get_text(strip=True)
        for variant in GROUP_VARIANTS:
            if variant in cell_text:
                target_col = idx
                break
        if target_col is not None:
            break

    if target_col is None:
        target_col = 2  # fallback
        logging.warning("Колонка группы не найдена, используется индекс 2 (третья)")

    logging.info(f"Колонка для группы: {target_col}")

    # Маппинг дней
    day_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    day_short = {'Понедельник': 'Пн', 'Вторник': 'Вт', 'Среда': 'Ср',
                 'Четверг': 'Чт', 'Пятница': 'Пт', 'Суббота': 'Сб'}

    schedule = {}
    current_day = None
    rowspan_remaining = 0

    for row in rows[1:]:
        cells = row.find_all(['td', 'th'])
        if not cells:
            continue

        # Определяем, новая ли это строка с днём
        if rowspan_remaining == 0:
            # Первая ячейка — день
            day_cell = cells[0]
            day_text = day_cell.get_text(strip=True)
            for d in day_names:
                if d in day_text:
                    current_day = d
                    break
            if not current_day:
                # Если день не распознан, пропускаем
                continue
            rowspan_remaining = int(day_cell.get('rowspan', 1))
            time_idx = 1          # вторая ячейка — время
            lesson_idx = target_col
        else:
            rowspan_remaining -= 1
            time_idx = 0          # время в первой ячейке (дня нет)
            # В таких строках колонка дня отсутствует, сдвиг на 1 влево
            lesson_idx = target_col - 1 if target_col > 0 else 0

        # Проверка наличия нужных ячеек
        if time_idx >= len(cells) or lesson_idx >= len(cells):
            continue

        time_cell = cells[time_idx]
        time_str = time_cell.get_text(strip=True)
        if not re.match(r'^\d{1,2}\.\d{2}$', time_str):
            continue  # пропускаем строки без времени

        lesson_cell = cells[lesson_idx]
        lesson_text = lesson_cell.get_text(strip=True)
        if not lesson_text or lesson_text == '-':
            continue

        # Иногда время дублируется в ячейке предмета, пропускаем
        if lesson_text == time_str:
            continue

        # Может быть несколько занятий через запятую или перенос строки
        # Разделяем по запятой или точке с запятой, но с учётом возможных внутренних запятых в названии
        # Упрощённо — разделяем по запятой, если после неё идёт время или аудитория
        parts = [lesson_text]
        if ',' in lesson_text:
            # Попытка разделить, но осторожно (может быть "Математика, лекция")
            # Лучше не делить — в нашем случае обычно одно занятие в ячейке
            pass

        for part in parts:
            part = part.strip()
            if not part:
                continue
            subject, teacher, room = parse_lesson(part)
            if subject is None:
                subject = part

            day_key = day_short.get(current_day, current_day[:2])
            schedule.setdefault(day_key, []).append({
                'time': time_str,
                'subject': subject,
                'teacher': teacher,
                'room': room
            })

    # Удаление дубликатов (одинаковое время + предмет в один день)
    for day in schedule:
        unique = []
        seen = set()
        for item in schedule[day]:
            key = (item['time'], item['subject'])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        schedule[day] = sorted(unique, key=lambda x: x['time'])

    # Логирование итогов
    for day in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб']:
        cnt = len(schedule.get(day, []))
        logging.info(f"{day}: {cnt} пар")

    if not schedule:
        raise ValueError("Расписание пустое — проверьте URL и колонку")

    return schedule

# ---------- Определение чётности недели ----------
def get_week_parity(date: datetime.date) -> str:
    """Возвращает 'нечётная' или 'чётная'."""
    delta = date - SEMESTER_START.date()
    week_number = delta.days // 7 + 1
    return "нечётная" if week_number % 2 == 1 else "чётная"

# ---------- Форматирование расписания на день ----------
def format_schedule_for_day(target_date):
    try:
        schedule = get_cached_schedule()
    except Exception as e:
        logging.exception("Ошибка загрузки расписания")
        return f"❌ Ошибка загрузки: {e}"

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
        room_str = f" в {l['room']}" if l['room'] else ""
        teacher_str = f"\n👨‍🏫 {l['teacher']}" if l['teacher'] else ""
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
                room_str = f" в {l['room']}" if l['room'] else ""
                teacher_str = f"\n👨‍🏫 {l['teacher']}" if l['teacher'] else ""
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

# ---------- Веб-сервер на aiohttp ----------
async def handle_health(request):
    return web.Response(text=f"Bot is running. Version: {VERSION}")

async def run_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logging.info("Web server started on port 8080")

# ---------- Главная функция ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    # Загружаем расписание при старте
    try:
        get_cached_schedule()
        logging.info("Расписание успешно загружено")
    except Exception as e:
        logging.error(f"Критическая ошибка загрузки: {e}")

    # Запускаем веб-сервер и бота параллельно
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
