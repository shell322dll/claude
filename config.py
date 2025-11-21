import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "secret")
REDMINE_URL = os.getenv("REDMINE_URL", "https://redmine.tiutututut.eu")

# === ПОЛЬЗОВАТЕЛИ И ИХ API ТОКЕНЫ ===
USER_CONFIGS = {
    1714290024: {
        "name": "Хозяин",
        "api_token": "hui"
    },
    969876137: {
        "name": "Василе",
        "api_token": "tebe"
    },
    3946811090: {
        "name": "Андрей",
        "api_token": "mudak"
    }
}

# ID Сергея Пожарова для уведомлений
POZHAROV_USER_ID = 4109074132

# Список разрешённых пользователей (автоматически из USER_CONFIGS)
ALLOWED_USERS = list(USER_CONFIGS.keys())

# Дефолтный токен (на случай ошибки)
REDMINE_API_TOKEN = "govno"

# === ВАЛИДАЦИЯ СЕРИЙНЫХ НОМЕРОВ ===
ALLOWED_SERIAL_PREFIXES = ["PC", "CE"]

# === СТАТУСЫ ЗАДАЧ ===
STATUS_NEW = 1
STATUS_IN_PROGRESS = 2
STATUS_DONE = 3

# === ПРОКСИ-СЕРВЕР ===
PROXY_URL = "http://proxy-13-01:3128"
PROXY_USERNAME = "maaus"
PROXY_PASSWORD = "ajk3289fYH2"
PROXY_AUTH = f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@proxy-13-01:3128"