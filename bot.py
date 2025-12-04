import logging
import sys
import re
import aiohttp
import asyncio
import mimetypes
import xml.etree.ElementTree as ET
import json

from pathlib import Path
from typing import Optional, Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, TelegramObject
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from urllib.parse import quote
from config import (
    TELEGRAM_TOKEN, 
    REDMINE_URL, 
    REDMINE_API_TOKEN, 
    STATUS_IN_PROGRESS, 
    STATUS_DONE, 
    ALLOWED_USERS, 
    USER_CONFIGS, 
    POZHAROV_USER_ID,
    DEFECTS_JSON_PATH,
    # Новые константы для несоответствий:
    FIELD_SERIAL_NUMBER,
    FIELD_DEFECT_CODE,
    FIELD_CATEGORY,
    TRACKER_DEFECT_FIX,
    STATUS_NEW,
    PRIORITY_HIGH,
    CHECKLIST_DEFECT_HEADER,
    CHECKLIST_DEFECT_PHOTO,
    CHECKLIST_DEFECT_SUBTASK,
    CHECKLIST_DEFECT_RECHECK,
    CHECKLIST_SUBTASK_HEADER,
    CHECKLIST_SUBTASK_MOVE_TO_PROD,
    CHECKLIST_SUBTASK_FIX_PREFIX,
    CHECKLIST_SUBTASK_CHECK,
    CHECKLIST_SUBTASK_MOVE_TO_TEST
)
from analyzer_service_sn import service as sn_service, AnalyzeResult

# Загрузка справочника несоответствий
DEFECTS = []
try:
    defects_path = Path(__file__).parent / DEFECTS_JSON_PATH
    logging.info(f"Загрузка справочника из: {defects_path}")
    
    with open(defects_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        DEFECTS = data.get("defects", [])
    
    logging.info(f"✅ Загружено {len(DEFECTS)} кодов несоответствий")
    
    # ДОБАВЬ ЭТО ДЛЯ ПРОВЕРКИ:
    if len(DEFECTS) > 0:
        logging.info(f"Первый дефект: {DEFECTS[0]}")
    
except Exception as e:
    logging.error(f"❌ Ошибка загрузки defects.json: {e}")
    logging.error(f"Путь: {Path(__file__).parent / DEFECTS_JSON_PATH}")

# Защита от двойных нажатий
user_processing = {}  # {user_id: timestamp}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)

# Явно включаем логи для aiogram
logging.getLogger('aiogram').setLevel(logging.INFO)

# Добавляем тестовый лог
logging.info("=" * 50)
logging.info("Логирование настроено!")
logging.info("=" * 50)

def search_defects(query: str, limit: int = 10) -> list:
    """
    Ищет несоответствия по подстроке в description.
    Возвращает список: [{"code": "001", "description": "..."}, ...]
    """
    query_lower = query.lower().strip()
    
    # ДОБАВЬ ЭТИ СТРОКИ ДЛЯ ОТЛАДКИ:
    logging.info(f"[SEARCH] Запрос: '{query_lower}'")
    logging.info(f"[SEARCH] Всего дефектов в базе: {len(DEFECTS)}")
    
    if not query_lower:
        return []
    
    results = []
    for defect in DEFECTS:
        # ДОБАВЬ ЭТО:
        if len(results) == 0:  # Логируем только первые попытки
            logging.info(f"[SEARCH] Проверяю: '{defect['description'].lower()}'")
        
        if query_lower in defect["description"].lower():
            results.append(defect)
            logging.info(f"[SEARCH] Найдено совпадение: {defect['code']} - {defect['description']}")
            if len(results) >= limit:
                break
    
    logging.info(f"[SEARCH] Итого найдено: {len(results)}")
    return results

def calculate_deadline() -> str:
    """
    Возвращает дедлайн: +1 день, пропуск выходных.
    Формат: "YYYY-MM-DD"
    """
    from datetime import datetime, timedelta
    
    today = datetime.now()
    deadline = today + timedelta(days=1)
    
    # Если завтра суббота (weekday=5) → +3 дня (понедельник)
    if deadline.weekday() == 5:
        deadline = today + timedelta(days=3)
    
    # Если завтра воскресенье (weekday=6) → +2 дня (понедельник)
    elif deadline.weekday() == 6:
        deadline = today + timedelta(days=2)
    
    return deadline.strftime("%Y-%m-%d")

def get_user_api_token(user_id: int) -> str:
    """Получает API токен пользователя по его Telegram ID"""
    user_config = USER_CONFIGS.get(user_id)
    if user_config:
        return user_config["api_token"]
    return REDMINE_API_TOKEN  # Fallback на дефолтный токен

class AuthMiddleware(BaseMiddleware):
    """Middleware для проверки доступа пользователей"""
    
    def __init__(self, allowed_users: list):
        self.allowed_users = allowed_users
        super().__init__()
        
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        
        if user and user.id not in self.allowed_users:
            # Игнорируем сообщения от неавторизованных пользователей
            return
        
        return await handler(event, data)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
# Регистрация middleware для проверки пользователей
dp.message.middleware(AuthMiddleware(ALLOWED_USERS))
dp.callback_query.middleware(AuthMiddleware(ALLOWED_USERS))

OCR_SEMAPHORE = asyncio.Semaphore(1)
last_uploaded = {}

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

async def check_existing_defect(issue_id: str, serial: str, user_id: int) -> bool:
    """
    Проверяет есть ли уже зарегистрированное несоответствие для серийника.
    Возвращает True если есть (блокируем регистрацию).
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return False
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        
        # Ищем блок серийника
        in_serial_block = False
        for cl in root.findall("checklist"):
            subj = (cl.findtext("subject") or "").strip().lower()
            
            # Начало блока серийника
            if "проверка оборудования" in subj and serial.upper() in subj.upper():
                in_serial_block = True
                continue
            
            # Конец блока (новый серийник)
            if in_serial_block and "проверка оборудования" in subj:
                break
            
            # Проверяем наличие пункта "Завести подзадачу"
            if in_serial_block and "завести подзадачу" in subj:
                return True
        
        return False
    
    except Exception as e:
        logging.error(f"Ошибка check_existing_defect: {e}")
        return False
        
async def find_equipment_name(control_task_id: str, serial: str, user_id: int) -> dict:
    """
    Находит задачу производства с серийником.
    
    Логика поиска:
    1. Получаем задачу контроля
    2. Получаем её родителя
    3. Проверяем САМОГО РОДИТЕЛЯ
    4. Если не нашли - ищем среди siblings (подзадач родителя)
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        logging.info(f"[FIND] Ищем оборудование для S/N: {serial} в задаче контроля #{control_task_id}")
        
        # Получаем задачу контроля
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{REDMINE_URL}/issues/{control_task_id}.json",
                headers=headers,
                ssl=False
            ) as resp:
                if resp.status != 200:
                    logging.error(f"[FIND] Ошибка получения задачи контроля: HTTP {resp.status}")
                    return None
                control_data = await resp.json()
        
        logging.info(f"[FIND] Задача контроля получена: {control_data.get('issue', {}).get('subject', 'N/A')}")
        
        # Получаем родителя
        parent = control_data.get("issue", {}).get("parent")
        if not parent:
            logging.error(f"[FIND] У задачи контроля нет родителя!")
            return None
        
        parent_id = str(parent["id"])
        logging.info(f"[FIND] Родительская задача: #{parent_id}")
        
        # ===== СНАЧАЛА ПРОВЕРЯЕМ САМОГО РОДИТЕЛЯ =====
        
        logging.info(f"[FIND] Проверяю родителя #{parent_id}...")
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{REDMINE_URL}/issues/{parent_id}.json",
                headers=headers,
                ssl=False
            ) as resp:
                if resp.status == 200:
                    parent_data = await resp.json()
                    
                    # Проверяем серийник у родителя
                    result = await check_task_for_serial(parent_data, parent_id, serial, user_id)
                    if result:
                        return result
                    else:
                        logging.info(f"[FIND] Родитель не содержит S/N {serial}")
        
        # ===== ЕСЛИ НЕ НАШЛИ У РОДИТЕЛЯ - ИЩЕМ СРЕДИ SIBLINGS =====
        
        logging.info(f"[FIND] Ищу среди siblings (подзадач родителя)...")
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{REDMINE_URL}/issues.json?parent_id={parent_id}&status_id=*&limit=100",
                headers=headers,
                ssl=False
            ) as resp:
                if resp.status != 200:
                    logging.error(f"[FIND] Ошибка получения подзадач родителя: HTTP {resp.status}")
                    return None
                siblings_data = await resp.json()
        
        siblings = siblings_data.get("issues", [])
        logging.info(f"[FIND] Найдено подзадач родителя (siblings): {len(siblings)}")
        
        # Проверяем каждую подзадачу родителя
        for idx, sibling in enumerate(siblings):
            sibling_id = str(sibling["id"])
            sibling_subject = sibling.get("subject", "")
            
            logging.info(f"[FIND] Проверяю sibling [{idx+1}/{len(siblings)}] #{sibling_id}: {sibling_subject[:60]}...")
            
            # Пропускаем саму задачу контроля
            if sibling_id == control_task_id:
                logging.info(f"[FIND] → Пропускаю (это задача контроля)")
                continue
            
            # Получаем полную информацию о задаче
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{REDMINE_URL}/issues/{sibling_id}.json",
                    headers=headers,
                    ssl=False
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"[FIND] → Ошибка получения задачи: HTTP {resp.status}")
                        continue
                    task_data = await resp.json()
            
            # Проверяем серийник
            result = await check_task_for_serial(task_data, sibling_id, serial, user_id)
            if result:
                return result
        
        logging.error(f"[FIND] ❌ Задача производства с S/N {serial} не найдена")
        return None
    
    except Exception as e:
        logging.error(f"[FIND] Ошибка find_equipment_name: {e}", exc_info=True)
        return None


async def check_task_for_serial(task_data: dict, task_id: str, serial: str, user_id: int) -> dict:
    """
    Проверяет содержит ли задача нужный серийный номер.
    Если да - возвращает информацию об оборудовании.
    Если нет - возвращает None.
    """
    try:
        # Проверяем поле "Серийный номер"
        custom_fields = task_data.get("issue", {}).get("custom_fields", [])
        serial_field = next((f for f in custom_fields if f.get("id") == FIELD_SERIAL_NUMBER), None)
        
        if not serial_field:
            logging.info(f"[CHECK] → Поле 'Серийный номер' отсутствует")
            return None
        
        serial_value = serial_field.get("value", "").strip()
        logging.info(f"[CHECK] → Серийный номер: '{serial_value}'")
        
        # Проверяем вхождение (может быть несколько серийников через пробел)
        if serial.upper() not in serial_value.upper():
            logging.info(f"[CHECK] → Не совпадает")
            return None
        
        logging.info(f"[CHECK] ✅ СОВПАДЕНИЕ! Нашли задачу #{task_id}")
        
        # Извлекаем название
        subject = task_data["issue"]["subject"]
        logging.info(f"[CHECK] Название задачи: {subject}")
        
        import re
        match = re.search(r'\(([^()]+)\)\s*$', subject)
        if match:
            equipment_full = match.group(1)
            logging.info(f"[CHECK] Извлечено (вариант без вложенных скобок): '{equipment_full}'")
        else:
            logging.error(f"[CHECK] Не удалось извлечь название оборудования из '{subject}'")
            return None
        
        # Вариант 1: С вложенными скобками (Видеосервер RV-SE3700 (Сборка 26309) - 1 шт.)
        match = re.search(r'\(([^(]+\([^)]+\)[^)]*)\)\s*$', subject)

        if match:
            equipment_full = match.group(1)
            logging.info(f"[CHECK] Извлечено (вариант с вложенными скобками): '{equipment_full}'")
        else:
            # Вариант 2: Без вложенных скобок (Персональный компьютер для Борисова В.В. - 1 шт.)
            match = re.search(r'\(([^()]+)\)\s*$', subject)
            
            if match:
                equipment_full = match.group(1)
                logging.info(f"[CHECK] Извлечено (вариант без вложенных скобок): '{equipment_full}'")
            else:
                logging.error(f"[CHECK] Не удалось извлечь название оборудования из '{subject}'")
                return None
        
        equipment_full = match.group(1)
        logging.info(f"[CHECK] Извлечено: '{equipment_full}'")
        
        # Заменяем количество на "- 1 шт."
        equipment_name = re.sub(r'-\s*\d+\s*шт\.', '- 1 шт.', equipment_full)
        logging.info(f"[CHECK] Итоговое название: '{equipment_name}'")
        
        # Определяем категорию
        if serial.upper().startswith("PC"):
            category = "Рабочая станция"
        elif serial.upper().startswith("CE"):
            category = "Сервер"
        else:
            category = "Сервер"  # По умолчанию
        
        logging.info(f"[CHECK] Категория: {category}")
        
        # Получаем assigned_to
        assigned_to = task_data["issue"].get("assigned_to")
        assigned_to_id = assigned_to["id"] if assigned_to else None
        assigned_to_name = assigned_to["name"] if assigned_to else "не назначен"
        
        logging.info(f"[CHECK] Назначена: {assigned_to_name} (ID: {assigned_to_id})")
        
        return {
            "equipment_name": equipment_name,
            "assigned_to_id": assigned_to_id,
            "assigned_to_name": assigned_to_name,
            "category": category,
            "project_id": task_data["issue"]["project"]["id"]
        }
    
    except Exception as e:
        logging.error(f"[CHECK] Ошибка check_task_for_serial: {e}", exc_info=True)
        return None
       
async def recalculate_done_ratio(issue_id: str, user_id: int):
    """Пересчитывает и обновляет процент готовности задачи"""
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        # Получаем текущие чек-листы
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{REDMINE_URL}/issues/{issue_id}/checklists.xml",
                headers=headers,
                ssl=False
            ) as resp:
                if resp.status != 200:
                    return
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        total = 0
        done = 0
        
        for cl in root.findall("checklist"):
            subj = (cl.findtext("subject") or "").strip().lower()
            # Пропускаем заголовки (все варианты!)
            if ("проверка оборудования" in subj or 
                "комплектация оборудования" in subj or 
                "выдача готового" in subj or
                "переместить изделие в изолятор брака" in subj):
                continue
            
            total += 1
            is_done = cl.findtext("is_done") or "0"
            if is_done in ("1", "true"):
                done += 1
        
        # Вычисляем процент
        if total > 0:
            done_ratio = int((done / total) * 100)
            
            # Обновляем задачу
            payload = {
                "issue": {
                    "done_ratio": done_ratio
                }
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"{REDMINE_URL}/issues/{issue_id}.json",
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    ssl=False
                ) as resp:
                    if resp.status in (200, 204):
                        logging.info(f"Done ratio обновлён: {done_ratio}% для задачи #{issue_id}")
                    else:
                        logging.error(f"Ошибка обновления done_ratio: HTTP {resp.status}")
    
    except Exception as e:
        logging.error(f"Ошибка recalculate_done_ratio: {e}")

async def count_equipment_in_checklist(issue_id: str, user_id: int) -> int:
    """
    Считает количество единиц оборудования в чек-листе задачи.
    Логика: количество пунктов "Проверка оборудования <серийник>" (без "указать серийный номер").
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return 0
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        count = 0
        
        for cl in root.findall("checklist"):
            subj = (cl.findtext("subject") or "").strip().lower()
            
            # Проверяем: "Проверка оборудования" + НЕ содержит "указать серийный номер"
            if "проверка оборудования" in subj and "указать серийный номер" not in subj:
                count += 1
        
        return count
    
    except Exception as e:
        logging.error(f"Ошибка count_equipment_in_checklist: {e}")
        return 0

async def get_custom_field_id(issue_id: str, field_name: str, user_id: int) -> Optional[int]:
    """
    Получает ID кастомного поля по его названию.
    Возвращает ID или None, если не найдено.
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}.json"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    logging.error(f"Ошибка получения задачи: HTTP {resp.status}")
                    return None
                data = await resp.json()
        
        custom_fields = data.get("issue", {}).get("custom_fields", [])
        
        for field in custom_fields:
            if field.get("name", "").strip().lower() == field_name.strip().lower():
                return field.get("id")
        
        logging.warning(f"Поле '{field_name}' не найдено в задаче {issue_id}")
        return None
    
    except Exception as e:
        logging.error(f"Ошибка get_custom_field_id: {e}")
        return None

async def download_file_bytes(file_id: str) -> bytes:
    """Скачивает файл из Telegram по file_id."""
    file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(file_url, ssl=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Не удалось загрузить файл (HTTP {resp.status})")
            return await resp.read()

async def ocr_sn_text_by_file_id(file_id: str) -> str:
    """Распознаёт S/N и пароль BIOS из изображения."""
    try:
        img_bytes = await download_file_bytes(file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        if res.found:
            return f"🔍 Найден S/N: {res.serial}\n\n🔑 Пароль BIOS: {res.password}"
        else:
            return "🔍 Серийный номер на фото не найден."
    except Exception as e:
        logging.error(f"OCR error: {e}")
        return f"🔍 Ошибка распознавания S/N: {e}"
        
async def get_all_serials_from_checklist(issue_id: str, user_id: int) -> list:
    """
    Возвращает список ВСЕХ серийников из чек-листа задачи контроля.
    Формат: ["ABC001", "ABC002", "ABC003", ...]
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return []
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        serials = []
        
        for cl in root.findall("checklist"):
            subj = (cl.findtext("subject") or "").strip()
            subj_l = subj.lower()
            
            # Ищем заголовки "Проверка оборудования <S/N>"
            if ("проверка оборудования" in subj_l and 
                "указать серийный номер" not in subj_l):
                
                # Извлекаем серийник из названия
                serial = subj.replace("Проверка оборудования", "").strip()
                
                if serial and serial not in serials:
                    serials.append(serial)
        
        return serials
    
    except Exception as e:
        logging.error(f"Ошибка get_all_serials_from_checklist: {e}")
        return []

# ===================== КОМАНДЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Это бот для работы с Redmine + распознавание S/N.\n\n"
        "<b>📋 Redmine команды:</b>\n"
        "/s4 &lt;фраза&gt; — глобальный поиск задач\n"
        "/s5 &lt;фраза&gt; — поиск задач контроль\n"
        "/c &lt;номер&gt; — удалить чек-лист задачи\n"
        "/d [номер] — удалить последнее фото\n\n"
        "<b>🚨 Регистрация несоответствий:</b>\n"
        "Фото + подпись: <code>d номер_задачи</code>\n"
        "Пример: отправь фото дефекта с подписью <code>d 12345</code>\n\n"
        "<b>📸 Работа с фото:</b>\n"
        "• <b>номер задачи</b> — прикрепить к задаче\n"
        "• <b>.</b> (точка) — найти задачу контроля по S/N\n"
        "• <b>Х</b> — последнее фото для оборудования\n\n"
        "<b>💡 Совет:</b> отправляй фото как <b>файл</b> для лучшего распознавания!",
        parse_mode="HTML"
    )

@dp.message(Command("s4"))
async def search_global(message: types.Message):
    query_text = message.text[len("/s4 "):].strip()
    if not query_text:
        await message.answer("Укажи фразу: /s4 <фраза>")
        return
    await perform_search(message, query_text)
            
# ===== /s5 — умный поиск задач "Контроль" =====
@dp.message(Command("s5"))
async def search_control(message: types.Message):
    query_text = message.text[len("/s5 "):].strip()
    if not query_text:
        await message.answer("Укажи фразу: /s5 <фраза>")
        return

    headers = {"X-Redmine-API-Key": get_user_api_token(message.from_user.id)}
    search_url = f"{REDMINE_URL}/search.json?q={quote(query_text)}&limit=10&scope=issues"

    async with aiohttp.ClientSession() as session:
        try:
            # 1) Базовый поиск задач
            async with session.get(search_url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    await message.answer(f"Ошибка поиска: HTTP {resp.status}")
                    return
                data = await resp.json()

            results = data.get("results", [])
            if not results:
                await message.answer("Ничего не найдено.")
                return

            # Собираем ID найденных задач
            issue_ids = []
            for res in results:
                rel_url = res.get("url") or ""
                full_url = rel_url if rel_url.startswith("http") else f"{REDMINE_URL}{rel_url}"
                m = re.search(r"/issues/(\d+)", full_url)
                if m:
                    issue_id = m.group(1)
                    if issue_id not in issue_ids:
                        issue_ids.append(issue_id)

            if not issue_ids:
                await message.answer("Ничего не найдено.")
                return

            found_controls = []
            reported_ids = set()

            # === ПРОХОД 1: Подзадачи найденных задач ===
            for iid in issue_ids:
                url_children = f"{REDMINE_URL}/issues.json?parent_id={iid}&status_id=*&limit=100"
                async with session.get(url_children, headers=headers, ssl=False) as r:
                    if r.status != 200:
                        continue
                    j = await r.json()

                for ch in j.get("issues", []):
                    subj = (ch.get("subject") or "").strip()
                    if "контроль" in subj.lower():
                        cid = str(ch.get("id"))
                        if cid not in reported_ids:
                            found_controls.append({
                                "id": cid,
                                "subject": subj,
                                "url": f"{REDMINE_URL}/issues/{cid}",
                            })
                            reported_ids.add(cid)

            # Если нашли — выводим
            if found_controls:
                for item in found_controls:
                    text = f"🔎 {item['subject']} #{item['id']}"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=item['id'], url=item['url'])]
                    ])
                    await message.answer(text, reply_markup=kb)
                return

            # === ПРОХОД 2: Подзадачи родителя найденных задач ===
            parent_ids = []
            for iid in issue_ids:
                issue_url = f"{REDMINE_URL}/issues/{iid}.json"
                async with session.get(issue_url, headers=headers, ssl=False) as r:
                    if r.status != 200:
                        continue
                    issue_data = await r.json()

                parent = (issue_data.get("issue") or {}).get("parent")
                if parent:
                    parent_id = str(parent.get("id"))
                    if parent_id and parent_id not in parent_ids:
                        parent_ids.append(parent_id)

            # Ищем подзадачи родителей с "контроль"
            for pid in parent_ids:
                url_parent_children = f"{REDMINE_URL}/issues.json?parent_id={pid}&status_id=*&limit=100"
                async with session.get(url_parent_children, headers=headers, ssl=False) as r:
                    if r.status != 200:
                        continue
                    j = await r.json()

                for ch in j.get("issues", []):
                    subj = (ch.get("subject") or "").strip()
                    if "контроль" in subj.lower():
                        cid = str(ch.get("id"))
                        if cid not in reported_ids:
                            found_controls.append({
                                "id": cid,
                                "subject": subj,
                                "url": f"{REDMINE_URL}/issues/{cid}",
                            })
                            reported_ids.add(cid)

            # Если нашли — выводим
            if found_controls:
                for item in found_controls:
                    text = f"🔎 {item['subject']} #{item['id']}"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=item['id'], url=item['url'])]
                    ])
                    await message.answer(text, reply_markup=kb)
                return

            # === ПРОХОД 3: Сам родитель с "контроль" в названии ===
            for pid in parent_ids:
                parent_url = f"{REDMINE_URL}/issues/{pid}.json"
                async with session.get(parent_url, headers=headers, ssl=False) as r:
                    if r.status != 200:
                        continue
                    pd = await r.json()

                parent_issue = pd.get("issue") or {}
                parent_subject = (parent_issue.get("subject") or "").strip()

                if "контроль" in parent_subject.lower():
                    cid = str(parent_issue.get("id"))
                    if cid not in reported_ids:
                        found_controls.append({
                            "id": cid,
                            "subject": parent_subject,
                            "url": f"{REDMINE_URL}/issues/{cid}",
                        })
                        reported_ids.add(cid)

            # Финальный вывод
            if found_controls:
                for item in found_controls:
                    text = f"🔎 {item['subject']} #{item['id']}"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=item['id'], url=item['url'])]
                    ])
                    await message.answer(text, reply_markup=kb)
            else:
                await message.answer("Задачи контроля не найдены.")

        except Exception as e:
            logging.error(f"Ошибка /s5: {e}")
            await message.answer(f"Ошибка при поиске задач контроля:\n{e}")

# ===================== Поиск задачи контроля (как /s5) =====================

async def find_control_task(serial: str, user_id: int) -> Optional[dict]:
    """
    Ищет задачу контроля по серийному номеру (логика /s5).
    Возвращает: {"id": "12345", "subject": "...", "url": "..."}
    или None, если не найдено
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    search_url = f"{REDMINE_URL}/search.json?q={quote(serial)}&limit=10&scope=issues"

    async with aiohttp.ClientSession() as session:
        try:
            # 1) Базовый поиск
            async with session.get(search_url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            results = data.get("results", [])
            if not results:
                return None

            # Собираем ID задач
            issue_ids = []
            found_issues = []
            for res in results:
                title = res.get("title", "")
                rel_url = res.get("url") or ""
                full_url = rel_url if rel_url.startswith("http") else f"{REDMINE_URL}{rel_url}"
                m = re.search(r"/issues/(\d+)", full_url)
                if m:
                    issue_id = m.group(1)
                    if issue_id not in issue_ids:
                        issue_ids.append(issue_id)
                        found_issues.append({
                            "id": issue_id,
                            "title": title,
                            "url": full_url
                        })

            if not issue_ids:
                return None

            # === ПРОВЕРКА 0: Есть ли "Контроль" в найденных задачах? ===
            for issue in found_issues:
                if "контроль" in issue["title"].lower():
                    return {
                        "id": issue["id"],
                        "subject": issue["title"],
                        "url": issue["url"]
                    }

            # === ПРОХОД 1: Подзадачи найденных задач ===
            for iid in issue_ids:
                url_children = f"{REDMINE_URL}/issues.json?parent_id={iid}&status_id=*&limit=100"
                async with session.get(url_children, headers=headers, ssl=False) as r:
                    if r.status == 200:
                        j = await r.json()
                        for ch in j.get("issues", []):
                            subj = (ch.get("subject") or "").strip()
                            if "контроль" in subj.lower():
                                return {
                                    "id": str(ch["id"]),
                                    "subject": subj,
                                    "url": f"{REDMINE_URL}/issues/{ch['id']}"
                                }

            # === ПРОХОД 2: Подзадачи родителей ===
            parent_ids = []
            for iid in issue_ids:
                issue_url = f"{REDMINE_URL}/issues/{iid}.json"
                async with session.get(issue_url, headers=headers, ssl=False) as r:
                    if r.status == 200:
                        issue_data = await r.json()
                        parent = (issue_data.get("issue") or {}).get("parent")
                        if parent:
                            pid = str(parent.get("id"))
                            if pid and pid not in parent_ids:
                                parent_ids.append(pid)

            for pid in parent_ids:
                url_pc = f"{REDMINE_URL}/issues.json?parent_id={pid}&status_id=*&limit=100"
                async with session.get(url_pc, headers=headers, ssl=False) as r:
                    if r.status == 200:
                        j = await r.json()
                        for ch in j.get("issues", []):
                            subj = (ch.get("subject") or "").strip()
                            if "контроль" in subj.lower():
                                return {
                                    "id": str(ch["id"]),
                                    "subject": subj,
                                    "url": f"{REDMINE_URL}/issues/{ch['id']}"
                                }

            # === ПРОХОД 3: Сам родитель ===
            for pid in parent_ids:
                parent_url = f"{REDMINE_URL}/issues/{pid}.json"
                async with session.get(parent_url, headers=headers, ssl=False) as r:
                    if r.status == 200:
                        pd = await r.json()
                        parent_issue = pd.get("issue") or {}
                        parent_subject = (parent_issue.get("subject") or "").strip()
                        if "контроль" in parent_subject.lower():
                            return {
                                "id": str(parent_issue["id"]),
                                "subject": parent_subject,
                                "url": f"{REDMINE_URL}/issues/{pid}"
                            }

            return None

        except Exception as e:
            logging.error(f"Ошибка find_control_task: {e}")
            return None

# Функция поиска и скачивания ТЗ

async def find_and_get_tz_file(issue_id: str, user_id: int) -> Optional[dict]:
    """
    Ищет самый свежий файл ТЗ*.xlsx в задаче контроля, если не находит — ищет в родительской задаче.
    Возвращает: {"filename": "ТЗ_123.xlsx", "file_url": "https://..."} или None
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    async def search_tz_in_issue(task_id: str) -> Optional[dict]:
        """Ищет самый свежий ТЗ*.xlsx в конкретной задаче"""
        url = f"{REDMINE_URL}/issues/{task_id}.json?include=attachments"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, ssl=False) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            
            attachments = data.get("issue", {}).get("attachments", [])
            
            # Собираем все файлы ТЗ
            tz_files = []
            for att in attachments:
                filename = att.get("filename", "").strip()
                if filename.upper().startswith("ТЗ") and filename.lower().endswith(".xlsx"):
                    file_url = att.get("content_url", "")
                    if not file_url.startswith("http"):
                        file_url = f"{REDMINE_URL}{file_url}"
                    
                    tz_files.append({
                        "filename": filename,
                        "file_url": file_url,
                        "id": att.get("id"),
                        "created_on": att.get("created_on", "")  # Дата создания
                    })
            
            # Если найдены файлы ТЗ - возвращаем самый свежий
            if tz_files:
                # Сортируем по дате создания (самый новый - последний)
                latest_tz = max(tz_files, key=lambda x: x.get("created_on", ""))
                logging.info(f"Найден самый свежий файл ТЗ: {latest_tz['filename']} в задаче #{task_id}")
                return latest_tz
            
            return None
        
        except Exception as e:
            logging.error(f"Ошибка поиска ТЗ в задаче #{task_id}: {e}")
            return None
    
    try:
        # 1) Ищем в задаче контроля
        tz_file = await search_tz_in_issue(issue_id)
        if tz_file:
            return tz_file
        
        # 2) Если не нашли — ищем в родительской задаче
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{REDMINE_URL}/issues/{issue_id}.json", headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        
        parent = data.get("issue", {}).get("parent")
        if parent:
            parent_id = str(parent.get("id"))
            logging.info(f"Задача контроля #{issue_id} имеет родителя #{parent_id}, ищу ТЗ там")
            tz_file = await search_tz_in_issue(parent_id)
            if tz_file:
                return tz_file
        
        logging.warning(f"Файл ТЗ не найден ни в задаче #{issue_id}, ни в родительской")
        return None
    
    except Exception as e:
        logging.error(f"Ошибка find_and_get_tz_file: {e}")
        return None

async def download_tz_file(file_url: str, filename: str, user_id: int) -> Optional[bytes]:
    """Скачивает файл ТЗ из Redmine"""
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    logging.error(f"Ошибка скачивания ТЗ: HTTP {resp.status}")
                    return None
                
                file_data = await resp.read()
                logging.info(f"Файл {filename} успешно скачан ({len(file_data)} байт)")
                return file_data
    
    except Exception as e:
        logging.error(f"Ошибка скачивания файла ТЗ: {e}")
        return None

async def get_checklist_for_serial(issue_id: str, serial: str, user_id: int) -> Optional[str]:
    """
    Возвращает отформатированный чек-лист для конкретного серийника.
    Если все пункты отмечены → возвращает "✅ Данное оборудование прошло ОТК!"
    Если серийник не найден → возвращает None
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return None
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        
        for cl in root.findall("checklist"):
            checklist_items.append({
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
            })
        
        # Найти блок серийника
        serial_idx = None
        for idx, item in enumerate(checklist_items):
            subj = item["subject"]
            if ("проверка оборудования" in subj.lower() and 
                serial.upper() in subj.upper() and 
                "указать" not in subj.lower()):
                serial_idx = idx
                break
        
        if serial_idx is None:
            # Серийник не найден в чек-листе
            return None
        
        # Найти конец блока
        block_end_idx = len(checklist_items) - 1
        for idx in range(serial_idx + 1, len(checklist_items)):
            subj_l = checklist_items[idx]["subject"].lower()
            if "проверка оборудования" in subj_l:
                block_end_idx = idx - 1
                break
        
        # Собираем пункты блока (без заголовков)
        checklist_lines = []
        all_checked = True
        
        for idx in range(serial_idx + 1, block_end_idx + 1):
            item = checklist_items[idx]
            subj = item["subject"]
            subj_l = subj.lower()
            is_done = item["is_done"] in ("1", "true")
            
            # Пропускаем заголовки
            if ("проверка оборудования" in subj_l or 
                "комплектация оборудования" in subj_l or 
                "выдача готового" in subj_l or
                "переместить изделие в изолятор брака" in subj_l):
                continue
            
            # Сокращаем название (убираем "+прикрепить фото...+")
            import re
            short_name = re.sub(r'\s*\+[^+]+\+\s*', '', subj).strip()
            
            # Добавляем в список
            icon = "✅" if is_done else "❌"
            checklist_lines.append(f"{icon} {short_name}")
            
            if not is_done:
                all_checked = False
        
        # Если все отмечены → специальное сообщение
        if all_checked:
            return "✅ Данное оборудование прошло ОТК!"
        
        # Иначе → список пунктов
        if not checklist_lines:
            return None
        
        return "📋 Чек-лист для данного оборудования:\n\n" + "\n".join(checklist_lines)
    
    except Exception as e:
        logging.error(f"Ошибка get_checklist_for_serial: {e}")
        return None

# ===================== FSM для загрузки фото =====================

class UploadPhoto(StatesGroup):
    waiting_for_issue = State()

# ===== НОВЫЕ СОСТОЯНИЯ ДЛЯ РЕГИСТРАЦИИ НЕСООТВЕТСТВИЙ =====
class DefectRegistration(StatesGroup):
    waiting_for_serial = State()      # Выбор серийника
    waiting_for_cause = State()       # Ввод текста поиска причины
    waiting_for_photo = State()       # Ожидание дополнительных фото
    confirming = State()              # Финальное подтверждение

# ===================== Обработка входящих изображений =====================

@dp.message(lambda m: m.photo and (m.caption or "").strip().lower().startswith("d "))
async def handle_defect_photo(message: types.Message, state: FSMContext):
    """Фото с подписью 'd 12345' - регистрация несоответствия"""
    caption = message.caption.strip()
    parts = caption.split(maxsplit=1)
    
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: d <номер_задачи>")
        return
    
    issue_id = parts[1]
    photo = message.photo[-1]
    
    # Валидация задачи
    headers = {"X-Redmine-API-Key": get_user_api_token(message.from_user.id)}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{REDMINE_URL}/issues/{issue_id}.json",
                headers=headers,
                ssl=False
            ) as resp:
                if resp.status != 200:
                    await message.answer(f"❌ Задача #{issue_id} не найдена или нет доступа")
                    return
    except Exception as e:
        await message.answer(f"❌ Ошибка проверки задачи: {e}")
        return
    
    # Получаем серийники из чек-листа
    serials = await get_all_serials_from_checklist(issue_id, message.from_user.id)
    
    if not serials:
        await message.answer(f"❌ В задаче #{issue_id} нет оборудования в чек-листе")
        return
    
    # Сохраняем данные
    await state.update_data(
        issue_id=issue_id,
        photos=[photo.file_id],
        defects=[]
    )
    await state.set_state(DefectRegistration.waiting_for_serial)
    
    # Показываем серийники кнопками (по 5 шт)
    buttons = []
    for serial in serials:
        buttons.append([InlineKeyboardButton(
            text=serial,
            callback_data=f"defect_serial:{issue_id}:{serial}:{message.from_user.id}"
        )])
    
    # Добавляем пагинацию если > 5
    # (упрощённо - пока без пагинации)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons + [
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"defect_cancel:{message.from_user.id}")]
    ])
    
    await message.answer(
        f"🚨 Регистрация несоответствия\n"
        f"Задача: #{issue_id}\n\n"
        f"Выберите оборудование:",
        reply_markup=keyboard
    )
    
@dp.message(Command("test_defects"))
async def test_defects_command(message: types.Message):
    """Тестовая команда - проверка загрузки справочника"""
    await message.answer(
        f"📋 Загружено дефектов: {len(DEFECTS)}\n\n"
        f"Первые 5:\n" + "\n".join([
            f"{d['code']}: {d['description']}" 
            for d in DEFECTS[:5]
        ])
    )

@dp.message(lambda msg: msg.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    """Фото (Telegram сжимает). OCR -> логика по подписи."""
    photo = message.photo[-1]
    caption = (message.caption or "").strip()

    # === СЦЕНАРИЙ 1: Фото + "." → поиск задачи контроля ===
    if caption == ".":
        status_msg = await message.answer("⏳ Распознаю серийный номер...")
        
        # OCR
        img_bytes = await download_file_bytes(photo.file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        
        if not res.found:
            await status_msg.delete()
            await message.answer("❌ Серийный номер на фото не распознан.")
            return
        
        serial = res.serial
        password = res.password
        
        # Поиск задачи контроля
        control_task = await find_control_task(serial, message.from_user.id)
        
        if not control_task:
            await status_msg.delete()
            await message.answer(f"❌ Задача контроля для S/N {serial} не найдена.")
            return
        
        # Удаляем сообщение "Распознаю..."
        await status_msg.delete()
        
        # Сохраняем данные для callback
        await state.update_data(
            photo_id=photo.file_id,
            serial=serial,
            password=password,
            control_task_id=control_task["id"],
            mime_type="image/jpeg"
        )
        
        evangelion_serials = [
           "PCPPP033000349", "PCPPP033000350", "PCPPP033000351", 
           "PCPPP033000352", "PCPPP033000353", "PCPPP033000354", "PCPPP033000355"
        ]
        text = f"🔹 S/N: {serial}"
        if serial in evangelion_serials:
            text += "\n🤮 Evangelion 🤮"
        text += f"\n\n🔐 BIOS: {password}"
        
        if "CETOE2300" in serial.upper() or "CETOE2600" in serial.upper():
            text += "\n⚠️ Напоминание: необходимо наклеить транспортировочные пломбы!"
        
        # === НОВАЯ ЛОГИКА: ПОЛУЧАЕМ ЧЕК-ЛИСТ ===
        checklist_text = await get_checklist_for_serial(control_task["id"], serial, message.from_user.id)
        if checklist_text:
            text += f"\n\n{checklist_text}"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_sn:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
        
        # === ПОИСК И ОТПРАВКА ТЗ ===
        tz_status_msg = await message.answer("⏳ Ищу файл ТЗ...")
        
        tz_file = await find_and_get_tz_file(control_task["id"], message.from_user.id)
        
        if tz_file:
            # Скачиваем файл
            file_data = await download_tz_file(tz_file["file_url"], tz_file["filename"], message.from_user.id)
            
            if file_data:
                await tz_status_msg.delete()
                
                # Отправляем файл пользователю
                from aiogram.types import BufferedInputFile
                
                document = BufferedInputFile(file_data, filename=tz_file["filename"])
                await message.answer_document(
                    document=document,
                    #caption=f"📄 Техническое задание: {tz_file['filename']}"
                )
                logging.info(f"Файл ТЗ {tz_file['filename']} отправлен пользователю {message.from_user.id}")
            else:
                await tz_status_msg.edit_text("⚠️ Не удалось скачать файл ТЗ")
        else:
            await tz_status_msg.edit_text("📄 ТЗ не найдено")

        return

    # === СЦЕНАРИЙ 1.5: Фото + "Х" (русская) → последнее фото для оборудования ===
    if caption.upper() == "Х":
        status_msg = await message.answer("⏳ Распознаю серийный номер...")
        
        # OCR
        img_bytes = await download_file_bytes(photo.file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        
        if not res.found:
            await status_msg.delete()
            await message.answer("❌ Серийный номер на фото не распознан.")
            return
        
        serial = res.serial
        password = res.password
        
        # Поиск задачи контроля
        control_task = await find_control_task(serial, message.from_user.id)
        
        if not control_task:
            await status_msg.delete()
            await message.answer(f"❌ Задача контроля для S/N {serial} не найдена.")
            return
        
        await status_msg.delete()
        
        # Сохраняем данные для callback с флагом "final_photo"
        await state.update_data(
            photo_id=photo.file_id,
            serial=serial,
            password=password,
            control_task_id=control_task["id"],
            mime_type="image/jpeg",
            is_final_photo=True  # Флаг для последнего фото
        )
        
        text = f"🔹 S/N: {serial}\n\n🔐 BIOS: {password}"
        
        if serial.upper().startswith("CET"):
            text += "\n\n⚠️ Напоминание: проверить наличие крепёжных винтов для жёстких дисков!"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_final:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
        return

    # === СЦЕНАРИЙ 2: Фото + номер задачи → загрузка + умная логика чек-листа ===
    if caption.isdigit():
        await handle_photo_with_issue(message, photo, caption, "image/jpeg")
        return

    # === СЦЕНАРИЙ 3: Фото без подписи → запрашиваем номер ===
    await state.update_data(photo_id=photo.file_id, mime_type="image/jpeg")
    await state.set_state(UploadPhoto.waiting_for_issue)
    await message.answer("Укажи номер задачи (цифрами), '.' для автопоиска или 'Х' для последнего фото.")
    
@dp.message(lambda m: m.document and (m.document.mime_type or "").startswith("image/"))
async def handle_image_document(message: types.Message, state: FSMContext):
    """Документ-картинка (оригинал). Та же логика."""
    doc = message.document
    caption = (message.caption or "").strip()

    # === СЦЕНАРИЙ 1: Документ + "." ===
    if caption == ".":
        status_msg = await message.answer("⏳ Распознаю серийный номер...")
        
        img_bytes = await download_file_bytes(doc.file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        
        if not res.found:
            await status_msg.delete()
            await message.answer("❌ Серийный номер на фото не распознан.")
            return
        
        serial = res.serial
        password = res.password
        
        control_task = await find_control_task(serial, message.from_user.id)
        
        if not control_task:
            await status_msg.delete()
            await message.answer(f"❌ Задача контроля для S/N {serial} не найдена.")
            return
        
        await status_msg.delete()
        
        await state.update_data(
            photo_id=doc.file_id,
            serial=serial,
            password=password,
            control_task_id=control_task["id"],
            mime_type=doc.mime_type
        )
        
        text = f"🔹 S/N: {serial}\n\n🔐 BIOS: {password}"
        
        if "CETOE2300" in serial.upper() or "CETOE2600" in serial.upper():
            text += "\n⚠️ Напоминание: необходимо наклеить транспортировочные пломбы!"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_sn:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
        
        # === НОВАЯ ЛОГИКА: ПОИСК И ОТПРАВКА ТЗ ===
        tz_status_msg = await message.answer("⏳ Ищу файл ТЗ...")
        
        tz_file = await find_and_get_tz_file(control_task["id"], message.from_user.id)
        
        if tz_file:
            file_data = await download_tz_file(tz_file["file_url"], tz_file["filename"], message.from_user.id)
            
            if file_data:
                await tz_status_msg.delete()
                
                from aiogram.types import BufferedInputFile
                
                document = BufferedInputFile(file_data, filename=tz_file["filename"])
                await message.answer_document(
                    document=document,
                    #caption=f"📄 Техническое задание: {tz_file['filename']}"
                )
                logging.info(f"Файл ТЗ {tz_file['filename']} отправлен пользователю {message.from_user.id}")
            else:
                await tz_status_msg.edit_text("⚠️ Не удалось скачать файл ТЗ")
        else:
            await tz_status_msg.edit_text("📄 ТЗ не найдено")
        
        return

    # === СЦЕНАРИЙ 1.5: Документ + "Х" ===
    if caption.upper() == "Х":
        status_msg = await message.answer("⏳ Распознаю серийный номер...")
        
        img_bytes = await download_file_bytes(doc.file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        
        if not res.found:
            await status_msg.delete()
            await message.answer("❌ Серийный номер на фото не распознан.")
            return
        
        serial = res.serial
        password = res.password
        
        control_task = await find_control_task(serial, message.from_user.id)
        
        if not control_task:
            await status_msg.delete()
            await message.answer(f"❌ Задача контроля для S/N {serial} не найдена.")
            return
        
        await status_msg.delete()
        
        await state.update_data(
            photo_id=doc.file_id,
            serial=serial,
            password=password,
            control_task_id=control_task["id"],
            mime_type=doc.mime_type,
            is_final_photo=True
        )
        
        text = f"🔹 S/N: {serial}\n\n🔐 BIOS: {password}"
        
        if serial.upper().startswith("CET"):
            text += "\n\n⚠️ Напоминание: проверить наличие крепёжных винтов для жёстких дисков!"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_final:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
        return

    # === СЦЕНАРИЙ 2: Документ + номер ===
    if caption.isdigit():
        class DummyPhoto:
            def __init__(self, fid): self.file_id = fid
        await handle_photo_with_issue(message, DummyPhoto(doc.file_id), caption, doc.mime_type)
        return

    # === СЦЕНАРИЙ 3: Без подписи ===
    await state.update_data(photo_id=doc.file_id, mime_type=doc.mime_type)
    await state.set_state(UploadPhoto.waiting_for_issue)
    await message.answer("Укажи номер задачи (цифрами), '.' для автопоиска или 'Х' для последнего фото.")


@dp.message(UploadPhoto.waiting_for_issue)
async def process_issue_number(message: types.Message, state: FSMContext):
    """Пользователь ввёл номер задачи после фото."""
    text = message.text.strip()
    
    # Если ввели "." → запускаем автопоиск
    if text == ".":
        data = await state.get_data()
        file_id = data.get("photo_id")
        
        if not file_id:
            await message.answer("❌ Ошибка: файл не найден в памяти.")
            await state.clear()
            return
        
        status_msg = await message.answer("⏳ Распознаю серийный номер...")
        
        img_bytes = await download_file_bytes(file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        
        if not res.found:
            await status_msg.delete()
            await message.answer("❌ Серийный номер на фото не распознан.")
            await state.clear()
            return
        
        serial = res.serial
        password = res.password
        
        control_task = await find_control_task(serial, message.from_user.id)
        
        if not control_task:
            await status_msg.delete()
            await message.answer(f"❌ Задача контроля для S/N {serial} не найдена.")
            await state.clear()
            return
        
        await status_msg.delete()
        
        await state.update_data(
            serial=serial,
            password=password,
            control_task_id=control_task["id"]
        )
        
        text = f"🔹 S/N: {serial}\n\n🔐 BIOS: {password}"
        
        if "CETOE2300" in serial.upper() or "CETOE2600" in serial.upper():
            text += "\n⚠️ Напоминание: необходимо наклеить транспортировочные пломбы!"
        
        # === НОВАЯ ЛОГИКА: ПОЛУЧАЕМ ЧЕК-ЛИСТ ===
        checklist_text = await get_checklist_for_serial(control_task["id"], serial, message.from_user.id)
        if checklist_text:
            text += f"\n\n{checklist_text}"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_sn:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
        
        # === ПОИСК И ОТПРАВКА ТЗ ===
        tz_status_msg = await message.answer("⏳ Ищу файл ТЗ...")
        
        tz_file = await find_and_get_tz_file(control_task["id"], message.from_user.id)
        
        if tz_file:
            file_data = await download_tz_file(tz_file["file_url"], tz_file["filename"], message.from_user.id)
            
            if file_data:
                await tz_status_msg.delete()
                
                from aiogram.types import BufferedInputFile
                
                document = BufferedInputFile(file_data, filename=tz_file["filename"])
                await message.answer_document(
                    document=document,
                    #caption=f"📄 Техническое задание: {tz_file['filename']}"
                )
                logging.info(f"Файл ТЗ {tz_file['filename']} отправлен пользователю {message.from_user.id}")
            else:
                await tz_status_msg.edit_text("⚠️ Не удалось скачать файл ТЗ")
        else:
            await tz_status_msg.edit_text("📄 ТЗ не найдено")

        return
    
    # Если ввели "Х" → запускаем поиск для последнего фото
    if text.upper() == "Х":
        data = await state.get_data()
        file_id = data.get("photo_id")
        
        if not file_id:
            await message.answer("❌ Ошибка: файл не найден в памяти.")
            await state.clear()
            return
        
        status_msg = await message.answer("⏳ Распознаю серийный номер...")
        
        img_bytes = await download_file_bytes(file_id)
        async with OCR_SEMAPHORE:
            res: AnalyzeResult = await asyncio.to_thread(sn_service.analyze_bytes, img_bytes)
        
        if not res.found:
            await status_msg.delete()
            await message.answer("❌ Серийный номер на фото не распознан.")
            await state.clear()
            return
        
        serial = res.serial
        password = res.password
        
        control_task = await find_control_task(serial, message.from_user.id)
        
        if not control_task:
            await status_msg.delete()
            await message.answer(f"❌ Задача контроля для S/N {serial} не найдена.")
            await state.clear()
            return
        
        await status_msg.delete()
        
        await state.update_data(
            serial=serial,
            password=password,
            control_task_id=control_task["id"],
            is_final_photo=True
        )
        
        text = f"🔹 S/N: {serial}\n\n🔐 BIOS: {password}"
        
        if serial.upper().startswith("CET"):
            text += "\n\n⚠️ Напоминание: проверить наличие крепёжных винтов для жёстких дисков!"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_final:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
        return
    
    # Если ввели число → загружаем в задачу
    if not text.isdigit():
        await message.answer("Нужно указать номер задачи (числом), '.' для автопоиска или 'Х' для последнего фото.")
        return

    data = await state.get_data()
    file_id = data.get("photo_id")
    mime_type = data.get("mime_type", "image/jpeg")
    
    if not file_id:
        await message.answer("❌ Ошибка: файл не найден в памяти.")
        await state.clear()
        return

    class DummyPhoto:
        def __init__(self, fid): self.file_id = fid
    
    await handle_photo_with_issue(message, DummyPhoto(file_id), text, mime_type)
    await state.clear()

# ===================== Загрузка фото в Redmine (без логики чек-листа) =====================

async def upload_photo_to_redmine(message: types.Message, issue_id: str, photo: object, mime_type: str):
    """Загружает фото в Redmine без дополнительной логики."""
    try:
        api_token = get_user_api_token(message.from_user.id)
        logging.info(f"User ID: {message.from_user.id}")
        logging.info(f"API токен (первые 10 символов): {api_token[:10]}...")
        logging.info(f"Загружаю фото в задачу #{issue_id}")
        
        file = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        filename = file.file_path.split("/")[-1]

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, ssl=False) as resp:
                photo_data = await resp.read()
                logging.info(f"Размер фото: {len(photo_data)} байт")

            upload_url = f"{REDMINE_URL}/uploads.json"
            headers = {
                "X-Redmine-API-Key": api_token,
                "Content-Type": "application/octet-stream",
            }

            logging.info(f"Отправляю POST запрос в Redmine: {upload_url}")
            
            async with session.post(upload_url, headers=headers, data=photo_data, ssl=False) as resp:
                logging.info(f"Получен ответ от Redmine: HTTP {resp.status}")
                logging.info(f"Content-Type ответа: {resp.headers.get('Content-Type', 'неизвестно')}")
                
                # ПРОВЕРКА СТАТУСА:
                if resp.status not in (200, 201):
                    error_text = await resp.text()
                    logging.error(f"Redmine вернул ошибку!")
                    logging.error(f"HTTP статус: {resp.status}")
                    logging.error(f"Ответ сервера (первые 500 символов):")
                    logging.error(error_text[:500])
                    await message.answer(f"❌ Ошибка загрузки фото в Redmine: HTTP {resp.status}")
                    return
                
                # Проверяем content-type перед парсингом JSON:
                content_type = resp.headers.get('Content-Type', '')
                if 'application/json' not in content_type:
                    error_text = await resp.text()
                    logging.error(f"Redmine вернул HTML вместо JSON!")
                    logging.error(f"Content-Type: {content_type}")
                    logging.error(f"Ответ (первые 500 символов):")
                    logging.error(error_text[:500])
                    await message.answer("❌ Ошибка: Redmine вернул неожиданный формат. Проверь API токен!")
                    return
                
                upload_info = await resp.json()
                token = upload_info["upload"]["token"]
                logging.info(f"✅ Получен токен загрузки: {token[:20]}...")

            ct = mime_type or "application/octet-stream"
            payload = {
                "issue": {
                    "uploads": [{"token": token, "filename": filename, "content_type": ct}]
                }
            }

            logging.info(f"Прикрепляю фото к задаче #{issue_id}")
            
            async with session.put(
                f"{REDMINE_URL}/issues/{issue_id}.json",
                headers={"X-Redmine-API-Key": api_token, "Content-Type": "application/json"},
                json=payload,
                ssl=False
            ) as resp:
                logging.info(f"Ответ на прикрепление: HTTP {resp.status}")
                
                if resp.status not in (200, 204):
                    error_text = await resp.text()
                    logging.error(f"Не удалось прикрепить фото к задаче")
                    logging.error(f"HTTP статус: {resp.status}")
                    logging.error(f"Ответ: {error_text[:500]}")
                    await message.answer(f"❌ Ошибка прикрепления фото: HTTP {resp.status}")
                    return
                
                logging.info(f"✅ Фото успешно прикреплено к задаче #{issue_id}")

            # Сохраняем для /d
            async with session.get(
                f"{REDMINE_URL}/issues/{issue_id}.json?include=attachments", 
                headers=headers, 
                ssl=False
            ) as resp2:
                if resp2.status == 200:
                    issue_data = await resp2.json()
                    attachments = issue_data.get("issue", {}).get("attachments", [])
                    if attachments:
                        last_uploaded[message.from_user.id] = {
                            "issue_id": issue_id,
                            "attachment_id": str(attachments[-1]["id"])
                        }

    except Exception as e:
        logging.error(f"Исключение в upload_photo_to_redmine: {e}", exc_info=True)
        raise

# ===================== Обновление чек-листа: первый шаг =====================

async def update_checklist_first_step(issue_id: str, serial: str, start_idx: int, checklist_items: list, user_id: int):
    """
    1. Переименовать пункт start_idx
    2. Поставить галочку на следующий пункт "Визуальный осмотр..."
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        async with aiohttp.ClientSession() as session:
            # Переименовать
            item = checklist_items[start_idx]
            checklist_el = ET.Element("checklist")
            ET.SubElement(checklist_el, "id").text = str(item["id"])
            ET.SubElement(checklist_el, "issue_id").text = str(item["issue_id"])
            ET.SubElement(checklist_el, "subject").text = f"Проверка оборудования {serial}"
            #ET.SubElement(checklist_el, "position").text = str(item["position"])
            
            payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
            async with session.put(f"{REDMINE_URL}/checklists/{item['id']}.xml", headers={**headers, "Content-Type": "application/xml"}, data=payload, ssl=False):
                pass
            
            # Поставить галочку на следующий
            if start_idx + 1 < len(checklist_items):
                next_item = checklist_items[start_idx + 1]
                if "визуальный осмотр" in next_item["subject"].lower():
                    checklist_el = ET.Element("checklist")
                    ET.SubElement(checklist_el, "id").text = str(next_item["id"])
                    ET.SubElement(checklist_el, "issue_id").text = str(next_item["issue_id"])
                    ET.SubElement(checklist_el, "subject").text = next_item["subject"]
                    ET.SubElement(checklist_el, "is_done").text = "1"
                    #ET.SubElement(checklist_el, "position").text = str(next_item["position"])
                    
                    payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
                    async with session.put(f"{REDMINE_URL}/checklists/{next_item['id']}.xml", headers={**headers, "Content-Type": "application/xml"}, data=payload, ssl=False):
                        pass
    
    except Exception as e:
        logging.error(f"Ошибка update_checklist_first_step: {e}")
        
# ===================== Callback: нажатие "Завершить проверку?" =====================

@dp.callback_query(lambda c: c.data.startswith("complete:"))
async def complete_check_callback(callback: CallbackQuery):
    """Пользователь нажал 'Завершить проверку?' — отмечаем оставшиеся пункты."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Ошибка: неверный формат данных", show_alert=True)
        return
    
    issue_id = parts[1]
    serial = parts[2]
    user_id = int(parts[3])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    await callback.answer("⏳ Завершаю проверку...")
    
    try:
        # 1) Отметить оставшиеся пункты блока
        marked_count = await mark_remaining_checklist_items(issue_id, serial, user_id)
        logging.info(f"Отмечено пунктов: {marked_count}")        
        # ===== ОТПРАВКА УВЕДОМЛЕНИЯ Сергею Пожарову =====
        try:
            notification_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Задача #{issue_id}", url=f"{REDMINE_URL}/issues/{issue_id}")]
            ])
            await bot.send_message(
                chat_id=POZHAROV_USER_ID,
                text=f"Задача контроля #{issue_id}\n🔹 S/N: {serial} упаковано и перемещается на склад.",
                reply_markup=notification_keyboard
            )
            logging.info(f"Уведомление отправлено Сергею Пожарову о задаче #{issue_id}")
            
            # ИЗМЕНИ ЭТУ СТРОКУ - используй user_id вместо callback.from_user.id:
            await bot.send_message(user_id, f"📬 Уведомление о возможности комплектации {serial} отправлено!")
            
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление Сергею Пожарову: {e}")
            
        # 2) Проверить: все ли чек-листы отмечены?
        all_complete = await check_all_checklists_complete(issue_id, user_id)
        logging.info(f"Все чек-листы заполнены: {all_complete}")
        
        # 3) Если все отмечены → обновить поля + сменить статус
        if all_complete:
            from config import STATUS_DONE
            headers = {
                "X-Redmine-API-Key": get_user_api_token(user_id),
                "Content-Type": "application/json"
            }
            
            # Получаем текущие значения полей
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{REDMINE_URL}/issues/{issue_id}.json", headers=headers, ssl=False) as resp:
                    if resp.status != 200:
                        logging.error(f"Ошибка получения задачи: HTTP {resp.status}")
                        return
                    issue_data = await resp.json()
            
            custom_fields_to_update = []
            current_fields = issue_data.get("issue", {}).get("custom_fields", [])
            
            # === Поле "Серийный номер" (id=11) ===
            serial_number_field = next((f for f in current_fields if f.get("id") == 11), None)
            if serial_number_field:
                current_value = serial_number_field.get("value", "").strip()
                # Заполнить прочерком ТОЛЬКО если пустое
                if not current_value:
                    custom_fields_to_update.append({"id": 11, "value": "-"})
                    logging.info("Поле 'Серийный номер' пустое → заполняем '-'")
                else:
                    logging.info(f"Поле 'Серийный номер' уже заполнено: '{current_value}'")
            
            # === Поле "Кол-во оборудования" (id=150) ===
            equipment_count = await count_equipment_in_checklist(issue_id, user_id)
            logging.info(f"Количество оборудования: {equipment_count}")
            
            if equipment_count > 0:
                custom_fields_to_update.append({"id": 150, "value": str(equipment_count)})
            
            # Формируем запрос
            payload = {
                "issue": {
                    "status_id": STATUS_DONE
                }
            }
            
            if custom_fields_to_update:
                payload["issue"]["custom_fields"] = custom_fields_to_update
            
            logging.info(f"Отправляем PUT запрос: {payload}")
            
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"{REDMINE_URL}/issues/{issue_id}.json",
                    headers=headers,
                    json=payload,
                    ssl=False
                ) as resp:
                    status = resp.status
                    response_text = await resp.text()
                    logging.info(f"Ответ Redmine: HTTP {status}, {response_text}")
                    
                    if status not in (200, 204):
                        logging.error(f"Ошибка смены статуса: HTTP {status}, {response_text}")
                        await bot.send_message(
                            callback.from_user.id,
                            f"⚠️ Ошибка смены статуса: HTTP {status}"
                        )
                        
        # Пересчитываем процент готовности
        await recalculate_done_ratio(issue_id, user_id)
        
        # 4) Удалить кнопку "Завершить проверку?"
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=issue_id, url=f"{REDMINE_URL}/issues/{issue_id}")]
        ]))
        
        # 5) Вывести результат
        if all_complete:
            await bot.send_message(
                callback.from_user.id,
                f"🎉 Задача контроля выполнена!"
            )
    
    except Exception as e:
        logging.error(f"Ошибка complete_check: {e}", exc_info=True)
        await bot.send_message(callback.from_user.id, f"❌ Ошибка при завершении проверки: {e}")
        
# ===================== Отметка оставшихся пунктов чек-листа =====================

async def mark_remaining_checklist_items(issue_id: str, serial: str, user_id: int) -> int:
    """
    Отмечает оставшиеся пункты блока серийника:
    - Проверка настроек BIOS и ОС
    - Функциональная проверка
    - Нагрузочное тестирование
    - Контроль комплектации прикрепить фото комплекта
    - Прикрепить лист выходного контроля
    - Упаковка оборудования
    - Перемещение готового оборудования на склад
    
    Возвращает количество отмеченных пунктов.
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    # Список пунктов для автоотметки (частичное совпадение)
    target_keywords = [
        "проверка настроек bios",
        "функциональная проверка",
        "проверка настроек операционной системы",
        "проверка настройки и лицензирования",
        "нагрузочное тестирование",
        "проведение нагрузочного тестирования",
        "контроль комплектации",
        "прикрепить лист выходного контроля",
        "упаковка оборудования",
        "контроль упаковки оборудования",
        "перемещение готового оборудования на склад",
    ]
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return 0
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        for cl in root.findall("checklist"):
            checklist_items.append({
                "id": cl.findtext("id"),
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
                "position": cl.findtext("position") or "0",
                "issue_id": cl.findtext("issue_id") or issue_id,
            })
        
        # Найти индекс блока серийника
        serial_idx = None
        for idx, item in enumerate(checklist_items):
            subj = item["subject"]
            if ("проверка оборудования" in subj.lower() and 
                serial.upper() in subj.upper() and 
                "указать" not in subj.lower()):
                serial_idx = idx
                break
        
        if serial_idx is None:
            return 0
        
        # Найти конец блока
        block_end_idx = len(checklist_items) - 1
        for idx in range(serial_idx + 1, len(checklist_items)):
            subj_l = checklist_items[idx]["subject"].lower()
            if ("проверка оборудования" in subj_l and 
                serial.upper() not in checklist_items[idx]["subject"].upper()):
                block_end_idx = idx - 1
                break
        
        # Отметить пункты из списка target_keywords
        marked = 0
        async with aiohttp.ClientSession() as session:
            for idx in range(serial_idx, block_end_idx + 1):
                item = checklist_items[idx]
                subj_l = item["subject"].lower()
                
                # Пропустить заголовки
                if ("проверка оборудования" in subj_l or 
                    "комплектация оборудования" in subj_l or 
                    "выдача готового" in subj_l or
                    "переместить изделие в изолятор брака" in subj_l):
                    continue
                
                # Пропустить уже отмеченные
                if item["is_done"] in ("1", "true"):
                    continue
                
                # Проверить: входит ли в список для автоотметки?
                should_mark = False
                for keyword in target_keywords:
                    if keyword in subj_l:
                        should_mark = True
                        break
                
                if not should_mark:
                    continue
                
                # Отметить пункт
                checklist_el = ET.Element("checklist")
                ET.SubElement(checklist_el, "id").text = str(item["id"])
                ET.SubElement(checklist_el, "issue_id").text = str(item["issue_id"])
                ET.SubElement(checklist_el, "subject").text = item["subject"]
                ET.SubElement(checklist_el, "is_done").text = "1"
                #ET.SubElement(checklist_el, "position").text = str(item["position"])
                
                payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
                update_url = f"{REDMINE_URL}/checklists/{item['id']}.xml"
                
                async with session.put(
                    update_url,
                    headers={**headers, "Content-Type": "application/xml"},
                    data=payload,
                    ssl=False
                ) as resp:
                    if resp.status in (200, 201, 422):
                        marked += 1
        
        # Пересчитываем процент готовности
        await recalculate_done_ratio(issue_id, user_id)
        return marked
    
    except Exception as e:
        logging.error(f"Ошибка mark_remaining: {e}")
        return 0
        
# ===================== Проверка: все ли чек-листы отмечены? =====================

async def check_all_checklists_complete(issue_id: str, user_id: int) -> bool:
    """
    Проверяет: все ли пункты чек-листа отмечены (кроме заголовков).
    Возвращает True, если все отмечены.
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return False
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        
        for cl in root.findall("checklist"):
            subj = (cl.findtext("subject") or "").strip().lower()
            is_done = cl.findtext("is_done") or "0"
            
            # Пропустить заголовки (все возможные варианты!)
            if ("проверка оборудования" in subj or 
                "комплектация оборудования" in subj or 
                "выдача готового" in subj or
                "переместить изделие в изолятор брака" in subj):
                continue
            
            # Если хоть один пункт не отмечен → False
            if is_done not in ("1", "true"):
                logging.info(f"[DEBUG] Неотмеченный пункт: '{cl.findtext('subject')}'")
                return False
        
        return True
    
    except Exception as e:
        logging.error(f"Ошибка check_all_checklists: {e}")
        return False

# ===================== НОВАЯ ЛОГИКА: работа с кнопками для чек-листа =====================

async def get_all_serials_with_unchecked_items(issue_id: str, user_id: int) -> list:
    """
    Возвращает список серийников, у которых есть неотмеченные пункты.
    Формат: [{"serial": "ABC123"}, {"serial": "DEF456"}, ...]
    
    НЕ включает серийники, у которых все пункты отмечены.
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return []
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        
        for cl in root.findall("checklist"):
            checklist_items.append({
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
            })
        
        # Найти все серийники в чек-листе
        serials_with_unchecked = []
        
        for idx, item in enumerate(checklist_items):
            subj = item["subject"]
            
            # Ищем заголовки "Проверка оборудования <S/N>"
            if ("проверка оборудования" in subj.lower() and 
                "указать серийный номер" not in subj.lower()):
                
                # Извлекаем серийник из названия
                # Формат: "Проверка оборудования ABC123"
                serial = subj.replace("Проверка оборудования", "").strip()
                
                if not serial:
                    continue
                
                # Проверяем, есть ли неотмеченные пункты у этого серийника
                # Ищем до следующего заголовка "Проверка оборудования"
                has_unchecked = False
                
                for check_idx in range(idx + 1, len(checklist_items)):
                    next_subj = checklist_items[check_idx]["subject"].lower()
                    
                    # Достигли следующего блока оборудования
                    if "проверка оборудования" in next_subj:
                        break
                    
                    # Пропускаем заголовки
                    if "комплектация оборудования" in next_subj or "выдача готового" in next_subj:
                        continue
                    
                    # Если нашли неотмеченный пункт
                    if checklist_items[check_idx]["is_done"] not in ("1", "true"):
                        has_unchecked = True
                        break
                
                # ДОБАВЛЯЕМ ТОЛЬКО если есть неотмеченные пункты
                if has_unchecked:
                    serials_with_unchecked.append({"serial": serial})
        
        return serials_with_unchecked
    
    except Exception as e:
        logging.error(f"Ошибка get_all_serials_with_unchecked_items: {e}")
        return []


async def get_available_buttons_for_serial(issue_id: str, serial: str, user_id: int) -> list:
    """
    Возвращает список доступных кнопок для серийника.
    Если пункт уже отмечен, кнопка не показывается.
    
    Возвращает: ["photo_po", "testing"] или подмножество
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return []
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        
        for cl in root.findall("checklist"):
            checklist_items.append({
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
            })
        
        # Найти блок серийника
        serial_idx = None
        for idx, item in enumerate(checklist_items):
            subj = item["subject"]
            if ("проверка оборудования" in subj.lower() and 
                serial.upper() in subj.upper() and 
                "указать" not in subj.lower()):
                serial_idx = idx
                break
        
        if serial_idx is None:
            return []
        
        # Найти конец блока
        block_end_idx = len(checklist_items) - 1
        for idx in range(serial_idx + 1, len(checklist_items)):
            subj_l = checklist_items[idx]["subject"].lower()
            if "проверка оборудования" in subj_l:
                block_end_idx = idx - 1
                break
        
        # Проверяем статус ключевых пунктов
        photo_po_checked = False
        testing_checked = False
        
        for idx in range(serial_idx + 1, block_end_idx + 1):
            item = checklist_items[idx]
            subj_l = item["subject"].lower()
            is_done = item["is_done"] in ("1", "true")
            
            if "проверка настройки и лицензирования" in subj_l and is_done:
                photo_po_checked = True
            
            if "проведение нагрузочного тестирования" in subj_l and is_done:
                testing_checked = True
        
        # Формируем список доступных кнопок
        available = []
        if not photo_po_checked:
            available.append("photo_po")
        if not testing_checked:
            available.append("testing")
        
        return available
    
    except Exception as e:
        logging.error(f"Ошибка get_available_buttons_for_serial: {e}")
        return []

async def mark_items_up_to_target(issue_id: str, serial: str, target_keyword: str, user_id: int) -> int:
    """
    Отмечает все пункты от "Визуальный осмотр" до выбранного пункта (включительно).
    Заполняет пробелы: если пункт уже отмечен, всё равно проходим дальше.
    
    target_keyword: 
    - "photo_po" (для "Фото ПО")
    - "testing" (для "Фото тестирования")
    
    Возвращает количество отмеченных пунктов.
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    # Список пунктов для отметки (по порядку)
    items_to_mark = [
        "визуальный осмотр",
        "функциональная проверка",
        "проверка настроек операционной системы",
    ]
    
    # Добавляем целевой пункт (ИСПРАВЛЕНО!)
    if target_keyword == "photo_po":
        items_to_mark.append("проверка настройки и лицензирования")  # С буквой И
    elif target_keyword == "testing":
        items_to_mark.append("проверка настройки и лицензирования")  # С буквой И
        items_to_mark.append("проведение нагрузочного тестирования")  # Со словом "Проведение"
    
    logging.info(f"[DEBUG] Целевые ключевые слова: {items_to_mark}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return 0
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        
        for cl in root.findall("checklist"):
            checklist_items.append({
                "id": cl.findtext("id"),
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
                "position": cl.findtext("position") or "0",
                "issue_id": cl.findtext("issue_id") or issue_id,
            })
        
        # Найти индекс блока серийника
        serial_idx = None
        for idx, item in enumerate(checklist_items):
            subj = item["subject"]
            if ("проверка оборудования" in subj.lower() and 
                serial.upper() in subj.upper() and 
                "указать" not in subj.lower()):
                serial_idx = idx
                logging.info(f"[DEBUG] Найден блок серийника на позиции {idx}: {subj}")
                break
        
        if serial_idx is None:
            logging.error(f"[DEBUG] Серийник {serial} не найден в чек-листе!")
            return 0
        
        # Найти конец блока
        block_end_idx = len(checklist_items) - 1
        for idx in range(serial_idx + 1, len(checklist_items)):
            subj_l = checklist_items[idx]["subject"].lower()
            if ("проверка оборудования" in subj_l and 
                serial.upper() not in checklist_items[idx]["subject"].upper()):
                block_end_idx = idx - 1
                break
        
        logging.info(f"[DEBUG] Блок серийника: позиции {serial_idx} - {block_end_idx}")
        
        # Отметить пункты из списка items_to_mark
        marked = 0
        async with aiohttp.ClientSession() as session:
            for idx in range(serial_idx + 1, block_end_idx + 1):
                item = checklist_items[idx]
                subj_l = item["subject"].lower()
                
                logging.info(f"[DEBUG] Проверяю пункт [{idx}]: '{item['subject']}' (is_done={item['is_done']})")
                
                # Пропустить заголовки
                if "проверка оборудования" in subj_l or "комплектация оборудования" in subj_l or "выдача готового" in subj_l:
                    logging.info(f"[DEBUG] → Пропущен (заголовок)")
                    continue
                
                # Проверить: входит ли в список для отметки?
                should_mark = False
                matched_keyword = None
                for keyword in items_to_mark:
                    if keyword in subj_l:
                        should_mark = True
                        matched_keyword = keyword
                        break
                
                if not should_mark:
                    logging.info(f"[DEBUG] → Пропущен (не входит в список)")
                    continue
                
                logging.info(f"[DEBUG] → Совпадение по ключевому слову: '{matched_keyword}'")
                
                # Отметить пункт (даже если уже отмечен)
                checklist_el = ET.Element("checklist")
                ET.SubElement(checklist_el, "id").text = str(item["id"])
                ET.SubElement(checklist_el, "issue_id").text = str(item["issue_id"])
                ET.SubElement(checklist_el, "subject").text = item["subject"]
                ET.SubElement(checklist_el, "is_done").text = "1"
                #ET.SubElement(checklist_el, "position").text = str(item["position"])
                
                payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
                update_url = f"{REDMINE_URL}/checklists/{item['id']}.xml"
                
                async with session.put(
                    update_url,
                    headers={**headers, "Content-Type": "application/xml"},
                    data=payload,
                    ssl=False
                ) as resp:
                    if resp.status in (200, 201, 422):
                        # Считаем только если реально изменили статус
                        was_unchecked = item["is_done"] not in ("1", "true")
                        if was_unchecked:
                            marked += 1
                            logging.info(f"[DEBUG] → Отмечен (было не отмечено)")
                        else:
                            logging.info(f"[DEBUG] → Переотмечен (уже было отмечено)")
                    else:
                        logging.error(f"[DEBUG] → Ошибка отметки: HTTP {resp.status}")
        
        # Пересчитываем процент готовности
        await recalculate_done_ratio(issue_id, user_id)
        
        logging.info(f"[DEBUG] ИТОГО отмечено новых пунктов: {marked}")
        return marked
    
    except Exception as e:
        logging.error(f"Ошибка mark_items_up_to_target: {e}")
        return 0

async def get_available_buttons_for_serial(issue_id: str, serial: str, user_id: int) -> list:
    """
    Возвращает список доступных кнопок для серийника.
    Если пункт уже отмечен, кнопка не показывается.
    
    Возвращает: ["photo_po", "testing"] или подмножество
    """
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return []
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        
        for cl in root.findall("checklist"):
            checklist_items.append({
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
            })
        
        # Найти блок серийника
        serial_idx = None
        for idx, item in enumerate(checklist_items):
            subj = item["subject"]
            if ("проверка оборудования" in subj.lower() and 
                serial.upper() in subj.upper() and 
                "указать" not in subj.lower()):
                serial_idx = idx
                break
        
        if serial_idx is None:
            return []
        
        # Найти конец блока
        block_end_idx = len(checklist_items) - 1
        for idx in range(serial_idx + 1, len(checklist_items)):
            subj_l = checklist_items[idx]["subject"].lower()
            if "проверка оборудования" in subj_l:
                block_end_idx = idx - 1
                break
        
        # Проверяем статус ключевых пунктов
        photo_po_checked = False
        testing_checked = False
        
        for idx in range(serial_idx + 1, block_end_idx + 1):
            item = checklist_items[idx]
            subj_l = item["subject"].lower()
            is_done = item["is_done"] in ("1", "true")
            
            if "проверка настройки и лицензирования" in subj_l and is_done:
                photo_po_checked = True
            
            if "проведение нагрузочного тестирования" in subj_l and is_done:
                testing_checked = True
        
        # Формируем список доступных кнопок
        available = []
        if not photo_po_checked:
            available.append("photo_po")
        if not testing_checked:
            available.append("testing")
        
        return available
    
    except Exception as e:
        logging.error(f"Ошибка get_available_buttons_for_serial: {e}")
        return []

# ===================== Вспомогательная функция: загрузка фото с умной логикой =====================

async def handle_photo_with_issue(message: types.Message, photo: object, issue_id: str, mime_type: str):
    """
    Обработка фото с указанным номером задачи:
    1. ВСЕГДА загружаем фото в Redmine
    2. ВСЕГДА пишем "✅ Фото успешно загружено"
    3. Показываем кнопки с серийниками (если есть неотмеченные пункты)
    """
    # 1. Загрузка фото
    await upload_photo_to_redmine(message, issue_id, photo, mime_type)
    
    # 2. Сообщение об успешной загрузке
    await message.answer(f"✅ Фото успешно загружено в задачу #{issue_id}")
    
    # 3. Получаем список серийников с неотмеченными пунктами
    serials = await get_all_serials_with_unchecked_items(issue_id, message.from_user.id)
    
    if not serials:
        # Нет серийников с неотмеченными пунктами → ничего не делаем
        return
    
    # 4. Показываем кнопки с серийниками
    buttons = []
    for s in serials:
        buttons.append([InlineKeyboardButton(
            text=s["serial"], 
            callback_data=f"select_serial:{issue_id}:{s['serial']}:{message.from_user.id}"
        )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    sent_message = await message.answer("Выберите оборудование для чек-листа:", reply_markup=keyboard)
    
    # === ТАЙМАУТ 15 СЕКУНД ===
    async def remove_buttons_after_timeout():
        await asyncio.sleep(15)  # 15 секунд
        try:
            await sent_message.delete()  # Удаляем всё сообщение
            logging.info(f"Сообщение с кнопками удалено по таймауту для задачи #{issue_id}")
        except Exception as e:
            # Сообщение могло быть удалено пользователем
            logging.debug(f"Не удалось удалить сообщение: {e}")
    
    # Запускаем таймаут в фоне
    asyncio.create_task(remove_buttons_after_timeout())

# ===================== Callback: нажатие "ВЕРНО?" (для обычного фото с ".") =====================

@dp.callback_query(lambda c: c.data.startswith("confirm_sn:"))
async def confirm_serial_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь нажал 'ВЕРНО?' — выполняем все действия."""
    user_id = int(callback.data.split(":")[1])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    data = await state.get_data()
    photo_id = data.get("photo_id")
    serial = data.get("serial")
    control_task_id = data.get("control_task_id")
    mime_type = data.get("mime_type", "image/jpeg")
    
    if not all([photo_id, serial, control_task_id]):
        await callback.answer("Ошибка: данные не найдены", show_alert=True)
        return
    
    await callback.answer("⏳ Проверяю серийный номер...")
    
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        async with aiohttp.ClientSession() as session:
            # === ПРОВЕРКА ДУБЛИКАТОВ В ЧЕК-ЛИСТЕ ===
            logging.info(f"Проверка дубликата S/N {serial} в задаче #{control_task_id}")
            
            async with session.get(f"{REDMINE_URL}/issues/{control_task_id}/checklists.xml", headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    xml_text = await resp.text()
                    root = ET.fromstring(xml_text)
                    
                    # Проверяем, есть ли уже этот серийник в чек-листе
                    for cl in root.findall("checklist"):
                        subj = (cl.findtext("subject") or "").strip()
                        
                        # Ищем пункты "Проверка оборудования <серийник>"
                        if ("проверка оборудования" in subj.lower() and 
                            serial.upper() in subj.upper() and 
                            "серийный номер" not in subj.lower()):
                            
                            logging.warning(f"Дубликат S/N {serial} найден в задаче #{control_task_id}")
                            
                            # Удаляем кнопку "ВЕРНО?"
                            await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text=control_task_id, url=f"{REDMINE_URL}/issues/{control_task_id}")]
                            ]))
                            
                            # Отправляем ошибку
                            await bot.send_message(
                                callback.from_user.id,
                                f"⚠️ Ошибка: оборудование с S/N {serial} уже добавлено в задачу #{control_task_id}!\n\n"
                                f"Фото не загружено."
                            )
                            await state.clear()
                            return
            
            logging.info(f"Дубликат не найден, загружаю фото для S/N {serial}")
            
            # === ЗАГРУЗКА ФОТО ===
            file = await bot.get_file(photo_id)
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
            filename = file.file_path.split("/")[-1]
            
            async with session.get(file_url, ssl=False) as resp:
                photo_data = await resp.read()
            
            upload_url = f"{REDMINE_URL}/uploads.json"
            async with session.post(upload_url, headers={**headers, "Content-Type": "application/octet-stream"}, data=photo_data, ssl=False) as resp:
                upload_info = await resp.json()
                token = upload_info["upload"]["token"]
            
            # === ПРИКРЕПЛЕНИЕ К ЗАДАЧЕ + СМЕНА СТАТУСА ===
            async with session.get(f"{REDMINE_URL}/issues/{control_task_id}.json", headers=headers, ssl=False) as resp:
                issue_data = await resp.json()
                status_name = issue_data["issue"]["status"]["name"].lower()
            
            payload = {
                "issue": {
                    "uploads": [{"token": token, "filename": filename, "content_type": mime_type}]
                }
            }
            if status_name == "новая задача":
                payload["issue"]["status_id"] = STATUS_IN_PROGRESS
            
            async with session.put(f"{REDMINE_URL}/issues/{control_task_id}.json", headers={**headers, "Content-Type": "application/json"}, json=payload, ssl=False) as resp:
                pass
            
            # === ОБНОВЛЕНИЕ ЧЕК-ЛИСТА ===
            async with session.get(f"{REDMINE_URL}/issues/{control_task_id}/checklists.xml", headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    xml_text = await resp.text()
                    root = ET.fromstring(xml_text)
                    checklist_items = []
                    for cl in root.findall("checklist"):
                        checklist_items.append({
                            "id": cl.findtext("id"),
                            "subject": (cl.findtext("subject") or "").strip(),
                            "is_done": cl.findtext("is_done") or "0",
                            "position": cl.findtext("position") or "0",
                            "issue_id": cl.findtext("issue_id") or control_task_id,
                        })
                    
                    # Найти "указать серийный номер"
                    for idx, item in enumerate(checklist_items):
                        if ("проверка оборудования" in item["subject"].lower() and 
                            "указать серийный номер" in item["subject"].lower()):
                            await update_checklist_first_step(control_task_id, serial, idx, checklist_items, user_id)
                            break
            
            # Удаляем кнопку "ВЕРНО?"
            await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=control_task_id, url=f"{REDMINE_URL}/issues/{control_task_id}")]
            ]))
            
            # Отправляем НОВОЕ сообщение
            await bot.send_message(callback.from_user.id, f"✅ Фото успешно загружено в задачу #{control_task_id}")
            await state.clear()
    
    except Exception as e:
        logging.error(f"Ошибка confirm_sn: {e}", exc_info=True)
        await bot.send_message(callback.from_user.id, f"❌ Ошибка: {e}")

# ===================== Callback: нажатие "ВЕРНО?" (для последнего фото с "Х") =====================

@dp.callback_query(lambda c: c.data.startswith("confirm_final:"))
async def confirm_final_photo_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь нажал 'ВЕРНО?' для последнего фото — загружаем и отмечаем всё."""
    user_id = int(callback.data.split(":")[1])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    data = await state.get_data()
    photo_id = data.get("photo_id")
    serial = data.get("serial")
    control_task_id = data.get("control_task_id")
    mime_type = data.get("mime_type", "image/jpeg")
    
    if not all([photo_id, serial, control_task_id]):
        await callback.answer("Ошибка: данные не найдены", show_alert=True)
        return
    
    await callback.answer("⏳ Загружаю фото и завершаю проверку...")
    
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1) Загрузка фото
            file = await bot.get_file(photo_id)
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
            filename = file.file_path.split("/")[-1]
            
            async with session.get(file_url, ssl=False) as resp:
                photo_data = await resp.read()
            
            upload_url = f"{REDMINE_URL}/uploads.json"
            async with session.post(upload_url, headers={**headers, "Content-Type": "application/octet-stream"}, data=photo_data, ssl=False) as resp:
                upload_info = await resp.json()
                token = upload_info["upload"]["token"]
            
            # 2) Прикрепление к задаче
            payload = {
                "issue": {
                    "uploads": [{"token": token, "filename": filename, "content_type": mime_type}]
                }
            }
            
            async with session.put(f"{REDMINE_URL}/issues/{control_task_id}.json", headers={**headers, "Content-Type": "application/json"}, json=payload, ssl=False) as resp:
                pass
        
        # 3) Сообщение об успешной загрузке
        await bot.send_message(callback.from_user.id, f"✅ Фото успешно загружено в задачу #{control_task_id}")
        
        # 4) Отметить оставшиеся пункты чек-листа
        marked_count = await mark_remaining_checklist_items(control_task_id, serial, user_id)
        logging.info(f"Отмечено пунктов: {marked_count}")
        
        # 5) Отправка уведомления Сергею Пожарову
        try:
            notification_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Задача #{control_task_id}", url=f"{REDMINE_URL}/issues/{control_task_id}")]
            ])
            logging.info(f"Отправляю уведомление Сергею Пожарову о задаче #{control_task_id}, S/N: {serial}")
            
            await bot.send_message(
                chat_id=POZHAROV_USER_ID,
                text=f"Задача контроля #{control_task_id}\n🔹 S/N: {serial} упаковано и перемещается на склад.",
                reply_markup=notification_keyboard
            )
            logging.info(f"✅ Уведомление Сергею отправлено! Теперь отправляю пользователю {user_id}")
            await bot.send_message(user_id, f"📬 Уведомление о возможности комплектации {serial} отправлено!")
            logging.info(f"✅ Уведомление пользователю {user_id} отправлено!")
            
        except Exception as e:
            logging.error(f"❌ Ошибка отправки уведомлений: {e}", exc_info=True)
        
        # 6) Проверить все ли чек-листы отмечены
        all_complete = await check_all_checklists_complete(control_task_id, user_id)
        logging.info(f"Все чек-листы заполнены: {all_complete}")
        
        # 7) Если все отмечены → обновить поля + сменить статус + 🎉 САЛЮТ
        if all_complete:
            from config import STATUS_DONE
            headers_json = {
                "X-Redmine-API-Key": get_user_api_token(user_id),
                "Content-Type": "application/json"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{REDMINE_URL}/issues/{control_task_id}.json", headers=headers_json, ssl=False) as resp:
                    if resp.status == 200:
                        issue_data = await resp.json()
                        
                        custom_fields_to_update = []
                        current_fields = issue_data.get("issue", {}).get("custom_fields", [])
                        
                        serial_number_field = next((f for f in current_fields if f.get("id") == 11), None)
                        if serial_number_field:
                            current_value = serial_number_field.get("value", "").strip()
                            if not current_value:
                                custom_fields_to_update.append({"id": 11, "value": "-"})
                        
                        equipment_count = await count_equipment_in_checklist(control_task_id, user_id)
                        if equipment_count > 0:
                            custom_fields_to_update.append({"id": 150, "value": str(equipment_count)})
                        
                        payload = {"issue": {"status_id": STATUS_DONE}}
                        if custom_fields_to_update:
                            payload["issue"]["custom_fields"] = custom_fields_to_update
                        
                        logging.info(f"Отправляем PUT запрос для завершения задачи: {payload}")
                        
                        async with session.put(
                            f"{REDMINE_URL}/issues/{control_task_id}.json",
                            headers=headers_json,
                            json=payload,
                            ssl=False
                        ) as resp:
                            if resp.status in (200, 204):
                                logging.info(f"Задача #{control_task_id} переведена в статус 'Выполнено'")
                                # 🎉 САЛЮТ!
                                await bot.send_message(callback.from_user.id, "🎉 Задача контроля выполнена!")
                            else:
                                response_text = await resp.text()
                                logging.error(f"Ошибка смены статуса: HTTP {resp.status}, {response_text}")
        
        # 8) Пересчитываем процент готовности
        await recalculate_done_ratio(control_task_id, user_id)
        
        # 9) Удаляем кнопку "ВЕРНО?"
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=control_task_id, url=f"{REDMINE_URL}/issues/{control_task_id}")]
        ]))
        
        await state.clear()
    
    except Exception as e:
        logging.error(f"Ошибка confirm_final: {e}", exc_info=True)
        await bot.send_message(callback.from_user.id, f"❌ Ошибка: {e}")

# ===================== УДАЛЕНИЕ ВЛОЖЕНИЯ =====================

@dp.message(Command("d"))
async def delete_command(message: types.Message):
    args = message.text.split(maxsplit=1)
    issue_id = None
    if len(args) > 1 and args[1].isdigit():
        issue_id = args[1]

    attachment_id = None

    if issue_id:
        headers = {"X-Redmine-API-Key": get_user_api_token(message.from_user.id)}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{REDMINE_URL}/issues/{issue_id}.json?include=attachments",
                                   headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    await message.answer(f"Не удалось получить вложения задачи #{issue_id} (HTTP {resp.status})")
                    return
                issue_data = await resp.json()
                attachments = issue_data.get("issue", {}).get("attachments", [])
                if not attachments:
                    await message.answer(f"В задаче #{issue_id} нет вложений.")
                    return
                attachment_id = str(attachments[-1]["id"])
    else:
        user_last = last_uploaded.get(message.from_user.id)
        if not user_last:
            await message.answer("Нет фото для удаления.")
            return
        issue_id = user_last["issue_id"]
        attachment_id = user_last["attachment_id"]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"УДАЛИТЬ!", callback_data=f"delete:{issue_id}:{attachment_id}")]
        ]
    )
    await message.answer(f"Удалить фото из задачи #{issue_id}?", reply_markup=keyboard)

# ===================== CALLBACK HANDLERS ДЛЯ РАБОТЫ С ЧЕК-ЛИСТОМ =====================

@dp.callback_query(lambda c: c.data.startswith("select_serial:"))
async def select_serial_callback(callback: CallbackQuery):
    """Пользователь выбрал серийник → показываем пункты для отметки."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Ошибка: неверный формат данных", show_alert=True)
        return
    
    issue_id = parts[1]
    serial = parts[2]
    user_id = int(parts[3])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    await callback.answer()
    
    # Получаем доступные кнопки
    available_buttons = await get_available_buttons_for_serial(issue_id, serial, user_id)
    
    if not available_buttons:
        await callback.message.delete()
        await bot.send_message(callback.from_user.id, f"Все пункты для S/N {serial} уже отмечены!")
        return
    
    # Формируем кнопки
    buttons = []
    
    if "photo_po" in available_buttons:
        buttons.append([InlineKeyboardButton(
            text="ПО видеонаблюдения", 
            callback_data=f"mark_item:{issue_id}:{serial}:photo_po:{user_id}"
        )])
    
    if "testing" in available_buttons:
        buttons.append([InlineKeyboardButton(
            text="Нагрузочное тестирование", 
            callback_data=f"mark_item:{issue_id}:{serial}:testing:{user_id}"
        )])
    
    buttons.append([InlineKeyboardButton(
        text="← Назад", 
        callback_data=f"back_to_serials:{issue_id}:{user_id}"
    )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # === УДАЛЯЕМ СТАРОЕ СООБЩЕНИЕ ===
    await callback.message.delete()
    
    # === ОТПРАВЛЯЕМ НОВОЕ СООБЩЕНИЕ С НОВЫМ ТАЙМЕРОМ ===
    sent_message = await bot.send_message(
        callback.from_user.id,
        f"Выберите пункт для отметки (S/N: {serial}):",
        reply_markup=keyboard
    )
    
    # === ТАЙМЕР 15 СЕКУНД ===
    async def remove_buttons_after_timeout():
        await asyncio.sleep(15)
        try:
            await sent_message.delete()
            logging.info(f"Сообщение с пунктами удалено по таймауту для S/N {serial}")
        except Exception as e:
            logging.debug(f"Не удалось удалить сообщение: {e}")
    
    asyncio.create_task(remove_buttons_after_timeout())

@dp.callback_query(lambda c: c.data.startswith("mark_item:"))
async def mark_checklist_item_callback(callback: CallbackQuery):
    """Пользователь выбрал пункт → отмечаем все до него включительно."""
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("Ошибка: неверный формат данных", show_alert=True)
        return
    
    issue_id = parts[1]
    serial = parts[2]
    target = parts[3]  # "photo_po" или "testing"
    user_id = int(parts[4])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    # === ЗАЩИТА ОТ ДВОЙНЫХ НАЖАТИЙ ===
    import time
    current_time = time.time()
    
    if user_id in user_processing:
        last_time = user_processing[user_id]
        if current_time - last_time < 3:  # 3 секунды между нажатиями
            await callback.answer("⏳ Подождите, предыдущая операция ещё выполняется...", show_alert=True)
            return
    
    user_processing[user_id] = current_time
    # === КОНЕЦ ЗАЩИТЫ ===
    
    await callback.answer("⏳ Отмечаю пункты...")
    
    try:
        # Определяем целевой пункт (ИСПРАВЛЕНО!)
        if target == "photo_po":
            item_name = "Проверка настройки и лицензирования ПО видеонаблюдения"
        else:  # testing
            item_name = "Проведение нагрузочного тестирования"
        
        # Отмечаем пункты (передаём target напрямую: "photo_po" или "testing")
        marked_count = await mark_items_up_to_target(issue_id, serial, target, user_id)
        
        # Удаляем меню с кнопками
        await callback.message.delete()
        
        # Отправляем ОДНО сообщение (убрали дублирование)
        await bot.send_message(
            callback.from_user.id,
            f"📋 Отмечен пункт чек-листа: {item_name} (S/N: {serial})"
        )
        
        logging.info(f"Отмечено {marked_count} пунктов для S/N {serial} в задаче #{issue_id}")
        
        # Очищаем блокировку
        user_processing.pop(user_id, None)
    
    except Exception as e:
        logging.error(f"Ошибка mark_item: {e}", exc_info=True)
        await bot.send_message(callback.from_user.id, f"❌ Ошибка при отметке пункта: {e}")
        user_processing.pop(user_id, None)

@dp.callback_query(lambda c: c.data.startswith("back_to_serials:"))
async def back_to_serials_callback(callback: CallbackQuery):
    """Кнопка "Назад" → возвращаемся к выбору серийника."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка: неверный формат данных", show_alert=True)
        return
    
    issue_id = parts[1]
    user_id = int(parts[2])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    await callback.answer()
    
    # Получаем список серийников заново
    serials = await get_all_serials_with_unchecked_items(issue_id, user_id)
    
    if not serials:
        await callback.message.delete()
        await bot.send_message(callback.from_user.id, "Все пункты отмечены!")
        return
    
    # Показываем кнопки с серийниками
    buttons = []
    for s in serials:
        buttons.append([InlineKeyboardButton(
            text=s["serial"], 
            callback_data=f"select_serial:{issue_id}:{s['serial']}:{user_id}"
        )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # === УДАЛЯЕМ СТАРОЕ СООБЩЕНИЕ ===
    await callback.message.delete()
    
    # === ОТПРАВЛЯЕМ НОВОЕ С НОВЫМ ТАЙМЕРОМ ===
    sent_message = await bot.send_message(
        callback.from_user.id,
        "Выберите оборудование для чек-листа:",
        reply_markup=keyboard
    )
    
    # === ТАЙМЕР 15 СЕКУНД ===
    async def remove_buttons_after_timeout():
        await asyncio.sleep(15)
        try:
            await sent_message.delete()
            logging.info(f"Сообщение с кнопками удалено по таймауту для задачи #{issue_id}")
        except Exception as e:
            logging.debug(f"Не удалось удалить сообщение: {e}")
    
    asyncio.create_task(remove_buttons_after_timeout())

@dp.callback_query(lambda c: c.data.startswith("delete:"))
async def confirm_delete(callback: CallbackQuery):
    _, issue_id, attachment_id = callback.data.split(":")
    headers = {"X-Redmine-API-Key": get_user_api_token(callback.from_user.id)}

    try:
        url = f"{REDMINE_URL}/attachments/{attachment_id}.json"
        logging.info(f"Попытка удаления вложения #{attachment_id} из задачи #{issue_id}")
        
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    logging.info(f"✅ Фото успешно удалено из задачи #{issue_id} (attachment_id: {attachment_id})")
                    await callback.message.edit_text(f"❌ Фото успешно удалено из задачи #{issue_id}")
                    last_uploaded.pop(callback.from_user.id, None)
                else:
                    logging.error(f"Ошибка удаления фото: HTTP {resp.status}")
                    await callback.message.edit_text(f"⚠️ Ошибка удаления фото: HTTP {resp.status}")
    except Exception as e:
        logging.error(f"Исключение при удалении фото: {e}", exc_info=True)
        await callback.message.edit_text(f"⚠️ Ошибка при удалении фото:\n{e}")

# ===================== КОМАНДА /c — УДАЛЕНИЕ ЧЕК-ЛИСТА =====================

@dp.message(Command("c"))
async def checklist_command(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /c <номер задачи>")
        return

    issue_id = args[1]
    headers = {"X-Redmine-API-Key": get_user_api_token(message.from_user.id)}

    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем чек-лист
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    await message.answer(f"Не удалось получить чек-лист задачи #{issue_id}: HTTP {resp.status}")
                    return
                xml_text = await resp.text()

            root = ET.fromstring(xml_text)
            checklist_ids = []
            
            # Собираем ID всех пунктов чек-листа
            for cl in root.findall("checklist"):
                cid = cl.findtext("id")
                if cid:
                    checklist_ids.append(cid)
            
            if not checklist_ids:
                await message.answer(f"В задаче #{issue_id} чек-лист пуст.")
                return
            
            # Подтверждение удаления
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"УДАЛИТЬ {len(checklist_ids)} пунктов чек-листа!", 
                        callback_data=f"delete_checklist:{issue_id}:{message.from_user.id}"
                    )]
                ]
            )
            
            await message.answer(
                f"⚠️ Вы уверены? Будет удалено {len(checklist_ids)} пунктов чек-листа из задачи #{issue_id}",
                reply_markup=keyboard
            )
    
    except Exception as e:
        logging.error(f"Ошибка получения чек-листа: {e}")
        await message.answer(f"Ошибка при получении чек-листа: {e}")


@dp.callback_query(lambda c: c.data.startswith("delete_checklist:"))
async def confirm_delete_checklist(callback: CallbackQuery):
    """Подтверждение удаления чек-листа."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка: неверный формат данных", show_alert=True)
        return
    
    issue_id = parts[1]
    user_id = int(parts[2])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    await callback.answer("⏳ Удаляю чек-лист...")
    
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем чек-лист заново
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    await callback.message.edit_text(f"❌ Ошибка получения чек-листа: HTTP {resp.status}")
                    return
                xml_text = await resp.text()
            
            root = ET.fromstring(xml_text)
            checklist_ids = []
            
            for cl in root.findall("checklist"):
                cid = cl.findtext("id")
                if cid:
                    checklist_ids.append(cid)
            
            if not checklist_ids:
                await callback.message.edit_text(f"Чек-лист в задаче #{issue_id} уже пуст.")
                return
            
            # Удаляем все пункты
            deleted_count = 0
            failed_count = 0
            
            for cid in checklist_ids:
                delete_url = f"{REDMINE_URL}/checklists/{cid}.xml"
                async with session.delete(delete_url, headers=headers, ssl=False) as resp:
                    if resp.status in (200, 204):
                        deleted_count += 1
                        logging.info(f"Удалён пункт чек-листа ID={cid} из задачи #{issue_id}")
                    else:
                        failed_count += 1
                        logging.error(f"Не удалось удалить пункт ID={cid}: HTTP {resp.status}")
            
            # Пересчитываем процент готовности
            await recalculate_done_ratio(issue_id, user_id)
            
            # Результат
            result_text = f"✅ Чек-лист задачи #{issue_id} удалён!\n\n"
            result_text += f"Удалено пунктов: {deleted_count}"
            
            if failed_count > 0:
                result_text += f"\n⚠️ Не удалось удалить: {failed_count}"
            
            await callback.message.edit_text(result_text)
            logging.info(f"Чек-лист задачи #{issue_id} удалён пользователем {user_id}")
    
    except Exception as e:
        logging.error(f"Ошибка удаления чек-листа: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Ошибка при удалении чек-листа: {e}")

# ===== РЕГИСТРАЦИЯ НЕСООТВЕТСТВИЙ: CALLBACKS =====

@dp.callback_query(lambda c: c.data.startswith("defect_cancel:"))
async def defect_cancel_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена регистрации"""
    user_id = int(callback.data.split(":")[1])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    await state.clear()
    await callback.message.delete()
    await callback.answer("Регистрация отменена")


@dp.callback_query(lambda c: c.data.startswith("defect_serial:"))
async def defect_select_serial_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал серийник"""
    parts = callback.data.split(":")
    issue_id = parts[1]
    serial = parts[2]
    user_id = int(parts[3])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    # Проверка существующего несоответствия
    has_defect = await check_existing_defect(issue_id, serial, user_id)
    
    if has_defect:
        await callback.message.edit_text(
            f"❌ Для оборудования {serial} уже зарегистрировано несоответствие!\n\n"
            f"Проверьте чек-лист задачи #{issue_id}"
        )
        await state.clear()
        return
    
    await callback.answer()
    
    # Сохраняем серийник
    await state.update_data(serial=serial)
    await state.set_state(DefectRegistration.waiting_for_cause)
    
    await callback.message.edit_text(
        f"🔹 Задача: #{issue_id}\n"
        f"🔹 S/N: {serial}\n"
        f"📸 Фото: прикреплено\n\n"
        f"Начните вводить причину несоответствия..."
    )


@dp.message(DefectRegistration.waiting_for_cause)
async def defect_search_cause(message: types.Message, state: FSMContext):
    """Пользователь ввёл текст поиска"""
    query = message.text.strip()
    
    if not query:
        await message.answer("Введите хотя бы несколько символов")
        return
    
    # Поиск
    results = search_defects(query, limit=10)
    
    if not results:
        await message.answer(
            f"❌ По запросу '{query}' ничего не найдено\n\n"
            f"Попробуйте другие слова"
        )
        return
    
    # Показываем результаты
    buttons = []
    for defect in results:
        buttons.append([InlineKeyboardButton(
            text=f"{defect['code']} - {defect['description']}",
            callback_data=f"defect_cause:{defect['code']}:{message.from_user.id}"
        )])
    
    buttons.append([InlineKeyboardButton(
        text="← Назад", 
        callback_data=f"defect_back_serial:{message.from_user.id}"
    )])
    buttons.append([InlineKeyboardButton(
        text="❌ Отменить",
        callback_data=f"defect_cancel:{message.from_user.id}"
    )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        f"Найдено: {len(results)} шт.\n\n"
        f"Выберите причину:",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data.startswith("defect_cause:"))
async def defect_select_cause_callback(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал причину"""
    parts = callback.data.split(":")
    code = parts[1]
    user_id = int(parts[2])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    # Находим описание
    defect = next((d for d in DEFECTS if d["code"] == code), None)
    if not defect:
        await callback.answer("Ошибка: код не найден", show_alert=True)
        return
    
    # Добавляем дефект в список
    data = await state.get_data()
    defects = data.get("defects", [])
    defects.append({
        "code": code,
        "description": defect["description"]
    })
    await state.update_data(defects=defects)
    
    await callback.answer()
    
    # Спрашиваем: ещё дефекты?
    buttons = [
        [InlineKeyboardButton(
            text="➕ Да, добавить ещё",
            callback_data=f"defect_more:yes:{user_id}"
        )],
        [InlineKeyboardButton(
            text="✅ Нет, создать подзадачу",
            callback_data=f"defect_more:no:{user_id}"
        )],
        [InlineKeyboardButton(
            text="❌ Отменить",
            callback_data=f"defect_cancel:{user_id}"
        )]
    ]
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    photos_count = len(data.get("photos", []))
    
    await callback.message.edit_text(
        f"✅ Несоответствие добавлено!\n\n"
        f"🔹 S/N: {data['serial']}\n"
        f"🔹 Причина: {defect['description']} ({code})\n"
        f"📸 Фото: {photos_count} шт.\n\n"
        f"Есть ещё несоответствия на этом оборудовании?",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data.startswith("defect_more:"))
async def defect_more_callback(callback: CallbackQuery, state: FSMContext):
    """Добавить ещё или создать подзадачу"""
    parts = callback.data.split(":")
    choice = parts[1]
    user_id = int(parts[2])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    if choice == "yes":
        # Добавить ещё дефект
        await callback.answer()
        await state.set_state(DefectRegistration.waiting_for_cause)
        
        data = await state.get_data()
        await callback.message.edit_text(
            f"🔹 S/N: {data['serial']}\n"
            f"🔹 Дефектов: {len(data['defects'])} шт.\n\n"
            f"Начните вводить следующую причину..."
        )
    
    else:
        # Создать подзадачу - показываем финальное подтверждение
        await show_final_confirmation(callback.message, state, user_id)

@dp.callback_query(lambda c: c.data.startswith("defect_confirm:"))
async def defect_confirm_callback(callback: CallbackQuery, state: FSMContext):
    """Финальное подтверждение"""
    parts = callback.data.split(":")
    action = parts[1]
    user_id = int(parts[2])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для тебя!", show_alert=True)
        return
    
    if action == "edit":
        # Вернуться к добавлению дефектов
        await callback.answer()
        await state.set_state(DefectRegistration.waiting_for_cause)
        
        data = await state.get_data()
        await callback.message.edit_text(
            f"🔹 S/N: {data['serial']}\n"
            f"🔹 Дефектов: {len(data['defects'])} шт.\n\n"
            f"Начните вводить причину..."
        )
    
    elif action == "create":
        # Создать подзадачу
        await callback.answer("⏳ Создаю подзадачу...")
        await create_defect_subtask(callback.message, state, user_id)
        
async def create_defect_subtask(message: types.Message, state: FSMContext, user_id: int):
    """Создаёт подзадачу на устранение несоответствий и обновляет чек-листы"""
    data = await state.get_data()
    
    issue_id = data["issue_id"]
    serial = data["serial"]
    defects = data["defects"]
    photos = data["photos"]
    equipment_info = data["equipment_info"]
    deadline = data["deadline"]
    
    try:
        headers = {
            "X-Redmine-API-Key": get_user_api_token(user_id),
            "Content-Type": "application/json"
        }
        
        # ===== 1. ФОРМИРУЕМ ДАННЫЕ ПОДЗАДАЧИ =====
        
        # Название
        subject = f"Устранение несоответствий {equipment_info['equipment_name']}"
        
        # Описание
        defects_list = "\n".join([
            f"{i+1}. {d['description']} ({d['code']})"
            for i, d in enumerate(defects)
        ])
        description = f"Устранить несоответствия:\n{defects_list}"
        
        # Коды через запятую
        defect_codes = ", ".join([d["code"] for d in defects])
        
        # Payload подзадачи
        subtask_payload = {
            "issue": {
                "project_id": equipment_info["project_id"],
                "parent_issue_id": int(issue_id),
                "subject": subject,
                "description": description,
                "tracker_id": TRACKER_DEFECT_FIX,
                "status_id": STATUS_NEW,
                "priority_id": PRIORITY_HIGH,
                "due_date": deadline,
                "custom_fields": [
                    {"id": FIELD_SERIAL_NUMBER, "value": serial},
                    {"id": FIELD_DEFECT_CODE, "value": defect_codes},
                    {"id": FIELD_CATEGORY, "value": equipment_info["category"]}
                ]
            }
        }
        
        # Добавляем assigned_to если есть
        if equipment_info.get("assigned_to_id"):
            subtask_payload["issue"]["assigned_to_id"] = equipment_info["assigned_to_id"]
        
        # ===== 2. СОЗДАЁМ ПОДЗАДАЧУ =====
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{REDMINE_URL}/issues.json",
                headers=headers,
                json=subtask_payload,
                ssl=False
            ) as resp:
                if resp.status not in (200, 201):
                    error_text = await resp.text()
                    logging.error(f"Ошибка создания подзадачи: {error_text}")
                    await message.edit_text(f"❌ Ошибка создания подзадачи: HTTP {resp.status}")
                    await state.clear()
                    return
                
                subtask_data = await resp.json()
                subtask_id = str(subtask_data["issue"]["id"])
                logging.info(f"✅ Создана подзадача #{subtask_id}")
        
        # ===== 3. ЗАГРУЖАЕМ ФОТО В ЗАДАЧУ КОНТРОЛЯ =====
        
        for photo_id in photos:
            try:
                await upload_photo_to_redmine_by_id(issue_id, photo_id, user_id)
            except Exception as e:
                logging.error(f"Ошибка загрузки фото: {e}")
        
        # ===== 4. СОЗДАЁМ ЧЕК-ЛИСТ В ПОДЗАДАЧЕ =====
        
        await create_subtask_checklist(subtask_id, serial, defects, user_id)
        
        # ===== 5. ОБНОВЛЯЕМ ЧЕК-ЛИСТ ЗАДАЧИ КОНТРОЛЯ =====
        
        await update_control_task_checklist(issue_id, serial, subtask_id, user_id)
        
        # ===== 6. ПЕРЕСЧИТЫВАЕМ ПРОЦЕНТ ГОТОВНОСТИ =====
        
        await recalculate_done_ratio(issue_id, user_id)
        
        # ===== 7. ПОКАЗЫВАЕМ РЕЗУЛЬТАТ =====
        
        result_text = (
            f"✅ Подзадача создана!\n\n"
            f"🔹 #{subtask_id}: {subject}\n"
            f"🔹 Назначена: {equipment_info.get('assigned_to_name', 'не назначен')}\n"
            f"🔹 Срок: {deadline}\n"
            f"🔹 Дефектов: {len(defects)} шт.\n"
            f"📸 Фото: {len(photos)} шт. прикреплено к задаче контроля"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Открыть подзадачу", url=f"{REDMINE_URL}/issues/{subtask_id}"),
                InlineKeyboardButton(text="Задача контроля", url=f"{REDMINE_URL}/issues/{issue_id}")
            ]
        ])
        
        await message.edit_text(result_text, reply_markup=keyboard)
        await state.clear()
    
    except Exception as e:
        logging.error(f"Ошибка create_defect_subtask: {e}", exc_info=True)
        await message.edit_text(f"❌ Ошибка при создании подзадачи: {e}")
        await state.clear()
        
async def create_subtask_checklist(subtask_id: str, serial: str, defects: list, user_id: int):
    """
    Создаёт чек-лист в подзадаче на устранение несоответствий.
    
    Структура:
    - Заголовок: "Устранение несоответствий {serial} (отв. производство/Сборщик ПК)"
    - "Переместить изделие на участок производства"
    - "Исправить несоответствие: {описание}" (для каждого дефекта)
    - "Провести проверку сборки и программного обеспечения"
    - "Переместить продукцию на участок тестирования"
    """
    headers = {
        "X-Redmine-API-Key": get_user_api_token(user_id),
        "Content-Type": "application/xml"
    }
    
    try:
        checklist_items = []
        position = 0
        
        # 1. Заголовок (с пробелом в начале)
        header = CHECKLIST_SUBTASK_HEADER.format(serial=serial)
        checklist_items.append({
            "subject": header,
            "is_done": "0",
            "position": position
        })
        position += 1
        
        # 2. Переместить на участок производства
        checklist_items.append({
            "subject": CHECKLIST_SUBTASK_MOVE_TO_PROD,
            "is_done": "0",
            "position": position
        })
        position += 1
        
        # 3. Исправить несоответствия (для каждого дефекта)
        for defect in defects:
            checklist_items.append({
                "subject": f"{CHECKLIST_SUBTASK_FIX_PREFIX}{defect['description']}",
                "is_done": "0",
                "position": position
            })
            position += 1
        
        # 4. Провести проверку
        checklist_items.append({
            "subject": CHECKLIST_SUBTASK_CHECK,
            "is_done": "0",
            "position": position
        })
        position += 1
        
        # 5. Переместить на тестирование
        checklist_items.append({
            "subject": CHECKLIST_SUBTASK_MOVE_TO_TEST,
            "is_done": "0",
            "position": position
        })
        
        # Создаём все пункты
        async with aiohttp.ClientSession() as session:
            for item in checklist_items:
                checklist_el = ET.Element("checklist")
                ET.SubElement(checklist_el, "issue_id").text = subtask_id
                ET.SubElement(checklist_el, "subject").text = item["subject"]
                ET.SubElement(checklist_el, "is_done").text = item["is_done"]
                ET.SubElement(checklist_el, "position").text = str(item["position"])
                
                payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
                
                async with session.post(
                    f"{REDMINE_URL}/issues/{subtask_id}/checklists.xml",
                    headers=headers,
                    data=payload,
                    ssl=False
                ) as resp:
                    if resp.status not in (200, 201):
                        logging.error(f"Ошибка создания пункта чек-листа: HTTP {resp.status}")
        
        logging.info(f"✅ Чек-лист создан для подзадачи #{subtask_id}")
    
    except Exception as e:
        logging.error(f"Ошибка create_subtask_checklist: {e}")
        
async def update_control_task_checklist(issue_id: str, serial: str, subtask_id: str, user_id: int):
    """
    Обновляет чек-лист задачи контроля:
    1. Отмечает пункты от "Визуальный осмотр" до "ПО видеонаблюдения"
    2. Вставляет 4 новых пункта после "Нагрузочное тестирование"
    3. Отмечает 2 из них сразу
    """
    headers = {
        "X-Redmine-API-Key": get_user_api_token(user_id),
        "Content-Type": "application/xml"
    }
    
    try:
        # Получаем чек-лист
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{REDMINE_URL}/issues/{issue_id}/checklists.xml",
                headers=headers,
                ssl=False
            ) as resp:
                if resp.status != 200:
                    logging.error(f"Ошибка получения чек-листа: HTTP {resp.status}")
                    return
                xml_text = await resp.text()
        
        root = ET.fromstring(xml_text)
        checklist_items = []
        
        for cl in root.findall("checklist"):
            checklist_items.append({
                "id": cl.findtext("id"),
                "subject": (cl.findtext("subject") or "").strip(),
                "is_done": cl.findtext("is_done") or "0",
                "position": int(cl.findtext("position") or "0"),
                "issue_id": cl.findtext("issue_id") or issue_id
            })
        
        # ===== 1. НАЙТИ БЛОК СЕРИЙНИКА =====
        
        serial_idx = None
        for idx, item in enumerate(checklist_items):
            subj_l = item["subject"].lower()
            if ("проверка оборудования" in subj_l and 
                serial.upper() in item["subject"].upper() and
                "указать" not in subj_l):
                serial_idx = idx
                break
        
        if serial_idx is None:
            logging.error(f"Серийник {serial} не найден в чек-листе")
            return
        
        # ===== 2. НАЙТИ ПОЗИЦИЮ ДЛЯ ВСТАВКИ =====
        
        insert_after_position = None
        auto_check_until_position = None
        
        for idx in range(serial_idx + 1, len(checklist_items)):
            item = checklist_items[idx]
            subj_l = item["subject"].lower()
            
            # Конец блока (новый серийник)
            if "проверка оборудования" in subj_l and serial.upper() not in item["subject"].upper():
                break
            
            # Пункт для автоотметки (последний)
            if "проверка настройки и лицензирования" in subj_l and "видеонаблюдения" in subj_l:
                auto_check_until_position = item["position"]
            
            # Пункт после которого вставляем
            if "проведение нагрузочного тестирования" in subj_l:
                insert_after_position = item["position"]
        
        if insert_after_position is None:
            logging.error("Не найден пункт 'Проведение нагрузочного тестирования'")
            return
        
        # ===== 3. ОТМЕТИТЬ ПУНКТЫ ОТ НАЧАЛА ДО "ПО ВИДЕОНАБЛЮДЕНИЯ" =====
        
        if auto_check_until_position:
            async with aiohttp.ClientSession() as session:
                for idx in range(serial_idx + 1, len(checklist_items)):
                    item = checklist_items[idx]
                    
                    # Пропускаем заголовки
                    subj_l = item["subject"].lower()
                    if ("проверка оборудования" in subj_l or
                        "комплектация оборудования" in subj_l or
                        "выдача готового" in subj_l):
                        continue
                    
                    # Отмечаем до нужного пункта включительно
                    if item["position"] <= auto_check_until_position:
                        if item["is_done"] not in ("1", "true"):
                            await mark_checklist_item(item["id"], item["issue_id"], item["subject"], user_id)
                    else:
                        break
        
        # ===== 4. ВСТАВИТЬ 4 НОВЫХ ПУНКТА =====
        
        new_items = [
            {
                "subject": CHECKLIST_DEFECT_HEADER,  # С пробелом в начале - заголовок
                "is_done": "0",
                "position": insert_after_position + 1
            },
            {
                "subject": CHECKLIST_DEFECT_PHOTO,
                "is_done": "1",  # Отмечаем сразу
                "position": insert_after_position + 2
            },
            {
                "subject": CHECKLIST_DEFECT_SUBTASK,
                "is_done": "1",  # Отмечаем сразу
                "position": insert_after_position + 3
            },
            {
                "subject": CHECKLIST_DEFECT_RECHECK,
                "is_done": "0",
                "position": insert_after_position + 4
            }
        ]
        
        async with aiohttp.ClientSession() as session:
            for new_item in new_items:
                checklist_el = ET.Element("checklist")
                ET.SubElement(checklist_el, "issue_id").text = issue_id
                ET.SubElement(checklist_el, "subject").text = new_item["subject"]
                ET.SubElement(checklist_el, "is_done").text = new_item["is_done"]
                ET.SubElement(checklist_el, "position").text = str(new_item["position"])
                
                payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
                
                async with session.post(
                    f"{REDMINE_URL}/issues/{issue_id}/checklists.xml",
                    headers=headers,
                    data=payload,
                    ssl=False
                ) as resp:
                    if resp.status not in (200, 201):
                        logging.error(f"Ошибка вставки пункта: HTTP {resp.status}")
        
        logging.info(f"✅ Чек-лист задачи контроля #{issue_id} обновлён")
    
    except Exception as e:
        logging.error(f"Ошибка update_control_task_checklist: {e}")


async def mark_checklist_item(item_id: str, issue_id: str, subject: str, user_id: int):
    """Отмечает один пункт чек-листа"""
    headers = {
        "X-Redmine-API-Key": get_user_api_token(user_id),
        "Content-Type": "application/xml"
    }
    
    try:
        checklist_el = ET.Element("checklist")
        ET.SubElement(checklist_el, "id").text = item_id
        ET.SubElement(checklist_el, "issue_id").text = issue_id
        ET.SubElement(checklist_el, "subject").text = subject
        ET.SubElement(checklist_el, "is_done").text = "1"
        
        payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
        
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{REDMINE_URL}/checklists/{item_id}.xml",
                headers=headers,
                data=payload,
                ssl=False
            ) as resp:
                if resp.status not in (200, 201, 422):
                    logging.error(f"Ошибка отметки пункта: HTTP {resp.status}")
    
    except Exception as e:
        logging.error(f"Ошибка mark_checklist_item: {e}")
        
async def upload_photo_to_redmine_by_id(issue_id: str, file_id: str, user_id: int):
    """Загружает фото в Redmine по file_id из Telegram"""
    headers = {"X-Redmine-API-Key": get_user_api_token(user_id)}
    
    try:
        # Скачиваем файл из Telegram
        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
        filename = file.file_path.split("/")[-1]
        
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, ssl=False) as resp:
                photo_data = await resp.read()
            
            # Загружаем в Redmine
            upload_url = f"{REDMINE_URL}/uploads.json"
            async with session.post(
                upload_url,
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=photo_data,
                ssl=False
            ) as resp:
                if resp.status not in (200, 201):
                    logging.error(f"Ошибка загрузки файла: HTTP {resp.status}")
                    return
                upload_info = await resp.json()
                token = upload_info["upload"]["token"]
            
            # Прикрепляем к задаче
            payload = {
                "issue": {
                    "uploads": [{"token": token, "filename": filename, "content_type": "image/jpeg"}]
                }
            }
            
            async with session.put(
                f"{REDMINE_URL}/issues/{issue_id}.json",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
                ssl=False
            ) as resp:
                if resp.status in (200, 204):
                    logging.info(f"✅ Фото прикреплено к задаче #{issue_id}")
    
    except Exception as e:
        logging.error(f"Ошибка upload_photo_to_redmine_by_id: {e}")

async def show_final_confirmation(message: types.Message, state: FSMContext, user_id: int):
    """Показывает финальное подтверждение перед созданием подзадачи"""
    data = await state.get_data()
    
    issue_id = data["issue_id"]
    serial = data["serial"]
    defects = data["defects"]
    photos = data["photos"]
    
    # Получаем название оборудования
    equipment_info = await find_equipment_name(issue_id, serial, user_id)
    
    if not equipment_info:
        await message.edit_text(
            f"❌ Ошибка: не найдена задача производства для S/N {serial}\n\n"
            f"Проверьте что серийник указан в задаче производства"
        )
        await state.clear()
        return
    
    # Формируем список дефектов
    defects_list = "\n".join([
        f"   {i+1}. {d['description']} ({d['code']})"
        for i, d in enumerate(defects)
    ])
    
    # Дедлайн
    deadline = calculate_deadline()
    
    # Сохраняем equipment_info
    await state.update_data(equipment_info=equipment_info, deadline=deadline)
    await state.set_state(DefectRegistration.confirming)
    
    buttons = [
        [InlineKeyboardButton(text="✅ Создать", callback_data=f"defect_confirm:create:{user_id}")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"defect_confirm:edit:{user_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"defect_cancel:{user_id}")]
    ]
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.edit_text(
        f"📋 Создать подзадачу на устранение несоответствий?\n\n"
        f"🔹 Задача: #{issue_id}\n"
        f"🔹 S/N: {serial}\n"
        f"🔹 Оборудование: {equipment_info['equipment_name']}\n"
        f"🔹 Несоответствий: {len(defects)} шт.\n"
        f"{defects_list}\n"
        f"📸 Фото: {len(photos)} шт.\n"
        f"🔹 Назначена: {equipment_info.get('assigned_to_name', 'не назначен')}\n"
        f"🔹 Срок: {deadline}\n",
        reply_markup=keyboard
    )

# ===================== ЗАПУСК БОТА =====================

if __name__ == "__main__":
    print("=" * 50)
    print("ФАЙЛ BOT.PY ЗАГРУЖЕН!")
    print("=" * 50)
    print("Бот запущен...")
    logging.info("Бот запускается...")
    asyncio.run(dp.start_polling(bot))