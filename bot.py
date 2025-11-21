import logging
import sys
import re
import aiohttp
import asyncio
import mimetypes
import xml.etree.ElementTree as ET

from typing import Optional, Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, TelegramObject
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from urllib.parse import quote
from config import TELEGRAM_TOKEN, REDMINE_URL, REDMINE_API_TOKEN, STATUS_IN_PROGRESS, STATUS_DONE, ALLOWED_USERS, USER_CONFIGS, POZHAROV_USER_ID
from analyzer_service_sn import service as sn_service, AnalyzeResult

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
            # Пропускаем заголовки
            if "проверка оборудования" in subj or "комплектация оборудования" in subj or "выдача готового" in subj:
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

# ===================== КОМАНДЫ =====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Это бот для работы с Redmine + распознавание S/N.\n\n"
        "<b>📋 Redmine команды:</b>\n"
        "/s4 &lt;фраза&gt; — глобальный поиск задач\n"
        "/s5 &lt;фраза&gt; — поиск задач контроль (подзадачи → родитель)\n"
        "/d [номер] — удалить последнее фото\n"
        "/c &lt;номер&gt; — показать чек-лист и отметить 'Упаковка'\n\n"
        "<b>📸 Работа с фото:</b>\n"
        "Отправь фото с подписью:\n"
        "• <b>номер задачи</b> — прикрепить к задаче\n"
        "• <b>.</b> (точка) — найти задачу контроля по S/N\n"
        "• <b>Х</b> (русская) — загрузить последнее фото для оборудования\n"
        "Если забыл номер — бот переспросит.\n\n"
        "<b>💡 Совет:</b> отправляй фото как <b>файл</b> (не сжатое) для лучшего распознавания!",
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

# ===================== FSM для загрузки фото =====================

class UploadPhoto(StatesGroup):
    waiting_for_issue = State()


# ===================== Обработка входящих изображений =====================

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
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_sn:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
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
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=control_task["id"], url=control_task["url"]),
                InlineKeyboardButton(text="ВЕРНО?", callback_data=f"confirm_sn:{message.from_user.id}")
            ]
        ])
        
        await message.answer(text, reply_markup=keyboard)
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
            ET.SubElement(checklist_el, "position").text = str(item["position"])
            
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
                    ET.SubElement(checklist_el, "position").text = str(next_item["position"])
                    
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
                if "проверка оборудования" in subj_l or "комплектация оборудования" in subj_l or "выдача готового" in subj_l:
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
                ET.SubElement(checklist_el, "position").text = str(item["position"])
                
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
            
            # Пропустить заголовки
            if "проверка оборудования" in subj or "комплектация оборудования" in subj or "выдача готового" in subj:
                continue
            
            # Если хоть один пункт не отмечен → False
            if is_done not in ("1", "true"):
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
                ET.SubElement(checklist_el, "position").text = str(item["position"])
                
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
        
        # 7) Если все отмечены → обновить поля + сменить статус
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
                        
                        async with session.put(
                            f"{REDMINE_URL}/issues/{control_task_id}.json",
                            headers=headers_json,
                            json=payload,
                            ssl=False
                        ) as resp:
                            if resp.status in (200, 204):
                                logging.info(f"Задача #{control_task_id} переведена в статус 'Выполнено'")
        
        # 8) Пересчитываем процент готовности
        await recalculate_done_ratio(control_task_id, user_id)
        
        # 9) Удаляем кнопку "ВЕРНО?"
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=control_task_id, url=f"{REDMINE_URL}/issues/{control_task_id}")]
        ]))
        
        # 10) Сообщение о завершении
        if all_complete:
            await bot.send_message(callback.from_user.id, f"🎉 Задача контроля выполнена!")
        
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


# ===================== КОМАНДА /c — ЧЕК-ЛИСТ =====================

@dp.message(Command("c"))
async def checklist_command(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /c <номер задачи>")
        return

    issue_id = args[1]
    headers = {"X-Redmine-API-Key": get_user_api_token(message.from_user.id)}

    url = f"{REDMINE_URL}/issues/{issue_id}/checklists.xml"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, ssl=False) as resp:
            if resp.status != 200:
                await message.answer(f"Не удалось получить чек-лист задачи #{issue_id}: HTTP {resp.status}")
                return
            xml_text = await resp.text()

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        await message.answer(f"Ошибка парсинга XML чек-листа: {e}")
        return

    items = []
    target_ids = []
    for cl in root.findall("checklist"):
        cid = cl.findtext("id")
        subj = cl.findtext("subject") or ""
        done = cl.findtext("is_done") or "0"
        position = cl.findtext("position") or "0"
        issueid_inner = cl.findtext("issue_id") or issue_id
        checked = done in ("true", "1")
        items.append(f"[{'✔' if checked else '✖'}] {subj} (id={cid})")
        if subj.strip() == "Упаковка оборудования":
            target_ids.append({"id": cid, "subject": subj, "position": position, "issue_id": issueid_inner})

    if not items:
        await message.answer(f"В задаче #{issue_id} чек-лист пуст.")
    else:
        await message.answer("Чек-лист:\n" + "\n".join(items))

    if not target_ids:
        await message.answer("Пункт «Упаковка оборудования» не найден в чек-листе.")
        return

    async with aiohttp.ClientSession() as session:
        for t in target_ids:
            cid = t["id"]
            checklist_el = ET.Element("checklist")
            ET.SubElement(checklist_el, "id").text = str(cid)
            ET.SubElement(checklist_el, "issue_id").text = str(t.get("issue_id", issue_id))
            ET.SubElement(checklist_el, "subject").text = t.get("subject", "Упаковка оборудования")
            ET.SubElement(checklist_el, "is_done").text = "1"
            ET.SubElement(checklist_el, "position").text = str(t.get("position", "0"))

            payload = ET.tostring(checklist_el, encoding="utf-8", method="xml")
            update_url = f"{REDMINE_URL}/checklists/{cid}.xml"

            try:
                async with session.put(update_url, headers={**headers, "Content-Type": "application/xml"},
                                       data=payload, ssl=False) as resp2:
                    if resp2.status in (200, 201, 422):
                        await message.answer(f"✓ Поставлена галочка: «Упаковка оборудования» (id={cid}) в задаче #{issue_id}")
                    else:
                        await message.answer(f"Ошибка при отметке пункта id={cid}: HTTP {resp2.status}")
            except Exception as e:
                await message.answer(f"Ошибка при запросе к {update_url}: {e}")


# ===================== ЗАПУСК БОТА =====================

if __name__ == "__main__":
    print("=" * 50)
    print("ФАЙЛ BOT.PY ЗАГРУЖЕН!")
    print("=" * 50)
    print("Бот запущен...")
    logging.info("Бот запускается...")
    asyncio.run(dp.start_polling(bot))