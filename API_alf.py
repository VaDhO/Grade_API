import logging

import requests

CONFIG = {
    "api_url": "https://api.itresume.ru/statistics/",
    "client": "Skillfactory",
    "client_key": "M2MGWS",
    "start_date": "2026-05-01 00:00:00.000000",
    "end_date": "2026-05-01 23:59:59.999999",  # Актуальные данные только до этой даты

    # Параметры вашей локальной БД PostgreSQL
    "db_host": "localhost",
    "db_port": "5432",
    "db_name": "Grade_database",  # Замените на имя вашей БД
    "db_user": "app_py_user",  # Замените на пользователя (например, postgres)
    "db_password": "1111",  # Замените на пароль
}

params = {
        "client": CONFIG["client"],
        "client_key": CONFIG["client_key"],
        "start": CONFIG["start_date"],
        "end": CONFIG["end_date"]
}

try:
    response = requests.get(CONFIG["api_url"], params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    print(data)
    print(f"Скачивание завершено. Всего получено записей: {len(data)}")
except requests.exceptions.RequestException as e:
    print(f"Ошибка доступа к API (Status Code или соединение): {e}", exc_info=True)
