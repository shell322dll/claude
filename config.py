import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "23485769283467981236491846")
REDMINE_URL = os.getenv("REDMINE_URL", "https://redmine.ru")

# === ПОЛЬЗОВАТЕЛИ И ИХ API ТОКЕНЫ ===
USER_CONFIGS = {
    17334545024: {
        "name": "Хозяин",
        "api_token": "23referfg34t43fsdf234rerwfref"
    },
    9657668837: {
        "name": "Василе",
        "api_token": "g45g345g34f3feffg34t34t"
    },
    39678611690: {
        "name": "Андрей",
        "api_token": "g45gh56he4rtg34r23rt453tg45g45rg"
    }
}

# ID Сергея Пожарова для уведомлений
POZHAROV_USER_ID = 4567787875

# ===== НЕСООТВЕТСТВИЯ =====
DEFECTS_JSON_PATH = "defects.json"

# ID полей
FIELD_SERIAL_NUMBER = 11
FIELD_DEFECT_CODE = 153
FIELD_DEFECT_COUNT = 152  # Добавьте эту строку
FIELD_CATEGORY = 91

# ID трекера и статуса
TRACKER_DEFECT_FIX = 95
STATUS_NEW = 1
PRIORITY_HIGH = 3

# Тексты чек-листа для несоответствий
CHECKLIST_DEFECT_HEADER = "Переместить изделие в изолятор брака (при выявлении несоответствия)"
CHECKLIST_DEFECT_PHOTO = "Зафиксировать несоответствие скриншотом или фото (приложить к задаче)"
CHECKLIST_DEFECT_SUBTASK = "Завести подзадачу для исправления несоответствия"
CHECKLIST_DEFECT_RECHECK = "Провести повторный технический контроль после исправления несоответствия"

# Тексты чек-листа для подзадачи
CHECKLIST_SUBTASK_HEADER = "Устранение несоответствий {serial} (отв. производство/Сборщик ПК)"
CHECKLIST_SUBTASK_MOVE_TO_PROD = "Переместить изделие на участок производства"
CHECKLIST_SUBTASK_FIX_PREFIX = "Исправить несоответствие: "
CHECKLIST_SUBTASK_CHECK = "Провести проверку сборки и программного обеспечения"
CHECKLIST_SUBTASK_MOVE_TO_TEST = "Переместить продукцию на участок тестирования"

# Пункты чек-листа для автоотметки (от начала до этого пункта включительно)
CHECKLIST_AUTO_CHECK_UNTIL = "проверка настройки и лицензирования по видеонаблюдения"

# Пункт после которого вставляем блок несоответствия
CHECKLIST_INSERT_AFTER = "проведение нагрузочного тестирования"

# Список разрешённых пользователей (автоматически из USER_CONFIGS)
ALLOWED_USERS = list(USER_CONFIGS.keys())

# Дефолтный токен (на случай ошибки)
REDMINE_API_TOKEN = "349f8y340987fh0934hf04237hy"

# === ВАЛИДАЦИЯ СЕРИЙНЫХ НОМЕРОВ ===
ALLOWED_SERIAL_PREFIXES = ["PC", "CE"]

# === СТАТУСЫ ЗАДАЧ ===
STATUS_NEW = 1
STATUS_IN_PROGRESS = 2
STATUS_DONE = 3

# === ПРОКСИ-СЕРВЕР ===
PROXY_URL = "http://proxy-13-01:3128"
PROXY_USERNAME = "IH3f8ue"
PROXY_PASSWORD = "4f98u0h204978fh02e9hfd0duio"
PROXY_AUTH = f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@proxy-13-01:3128"