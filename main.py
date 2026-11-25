import os
import glob
import logging
import requests
import ast
import datetime
from collections import defaultdict
from typing import List, Dict, Any

import psycopg2
from psycopg2.extras import execute_values

# Библиотеки для Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================
CONFIG = {
    "api_url": "https://api.itresume.ru/statistics/",
    "client": "Skillfactory",
    "client_key": "M2MGWS",
    "start_date": "2026-06-01 00:00:00.000000",
    "end_date": "2026-06-01 23:59:59.999999",  # Актуальные данные только до этой даты

    # Параметры вашей локальной БД PostgreSQL
    "db_host": "localhost",
    "db_port": "5432",
    "db_name": "Grade_database",  # Замените на имя вашей БД
    "db_user": "app_py_user",  # Замените на пользователя (например, postgres)
    "db_password": "1111",  # Замените на пароль


    # Google Sheets (Задание со звездочкой №1)
    "gs_credentials_file": "credentials.json",  # Файл ключа должен лежать рядом со скриптом
    "gs_spreadsheet_url": "https://docs.google.com/spreadsheets/d/1Zty6sjiRVImfpS9sDu0knrV5En_afKATG-I08Mbz8JU/edit?gid=0#gid=0/edit",  # Вставь свою ссылку
}


# ==============================================================================
# 1. НАСТРОЙКА ЛОГИРОВАНИЯ И РОТАЦИЯ
# ==============================================================================
def setup_logging():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    today = datetime.date.today()
    for filepath in glob.glob(os.path.join(log_dir, "log_*.txt")):
        try:
            filename = os.path.basename(filepath)
            date_str = filename.replace("log_", "").replace(".txt", "")
            file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            if (today - file_date).days > 3:
                os.remove(filepath)
        except Exception:
            pass

    log_file = os.path.join(log_dir, f"log_{today.strftime('%Y-%m-%d')}.txt")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    logging.info("Логирование инициализировано. Старые логи (> 3 дней) удалены.")


# ==============================================================================
# 2. ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ==============================================================================
def init_db():
    logging.info("Подключение к PostgreSQL и проверка структуры таблицы...")
    try:
        conn = psycopg2.connect(
            host=CONFIG["db_host"], port=CONFIG["db_port"],
            dbname=CONFIG["db_name"], user=CONFIG["db_user"], password=CONFIG["db_password"]
        )
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS grader_stats (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255),
                oauth_consumer_key VARCHAR(255),
                lis_result_sourcedid TEXT,
                lis_outcome_service_url TEXT,
                is_correct BOOLEAN,
                attempt_type VARCHAR(50),
                created_at TIMESTAMP
            );
        """)
        conn.commit()
        logging.info("Таблица 'grader_stats' успешно проверена/создана.")
        return conn
    except Exception as e:
        logging.error(f"Критическая ошибка при инициализации БД: {e}", exc_info=True)
        raise


# ==============================================================================
# 3. ПОЛУЧЕНИЕ И ВАЛИДАЦИЯ ДАННЫХ ИЗ API
# ==============================================================================
def fetch_and_parse_data() -> List[Dict[str, Any]]:
    logging.info("Начало скачивания данных с API...")
    params = {
        "client": CONFIG["client"], "client_key": CONFIG["client_key"],
        "start": CONFIG["start_date"], "end": CONFIG["end_date"]
    }

    try:
        response = requests.get(CONFIG["api_url"], params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        logging.info(f"Скачивание завершено. Всего получено записей: {len(data)}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка доступа к API: {e}", exc_info=True)
        return []

    parsed_data = []
    for idx, record in enumerate(data):
        try:
            passback_str = record.get("passback_params", "{}")
            try:
                passback_dict = ast.literal_eval(passback_str)
            except (ValueError, SyntaxError):
                passback_dict = {}

            if not isinstance(passback_dict, dict):
                passback_dict = {}

            user_id = str(record.get("lti_user_id", "")).strip()
            if not user_id:
                continue

            attempt_type = str(record.get("attempt_type", "")).strip().lower()
            if attempt_type not in ["run", "submit"]:
                continue

            is_correct_raw = record.get("is_correct")
            is_correct = None if is_correct_raw is None else bool(is_correct_raw)

            created_at_str = record.get("created_at")
            try:
                created_at = datetime.datetime.fromisoformat(created_at_str) if created_at_str else None
            except ValueError:
                continue

            parsed_data.append({
                "user_id": user_id,
                "oauth_consumer_key": str(passback_dict.get("oauth_consumer_key", "")).strip(),
                "lis_result_sourcedid": str(passback_dict.get("lis_result_sourcedid", "")).strip(),
                "lis_outcome_service_url": str(passback_dict.get("lis_outcome_service_url", "")).strip(),
                "is_correct": is_correct,
                "attempt_type": attempt_type,
                "created_at": created_at
            })
        except Exception as e:
            logging.error(f"Ошибка парсинга записи #{idx}: {e}")

    logging.info(f"Обработка данных завершена. Валидных записей: {len(parsed_data)}")
    return parsed_data


# ==============================================================================
# 4. ЗАГРУЗКА ДАННЫХ В БАЗУ
# ==============================================================================
def load_to_db(conn, data: List[Dict[str, Any]]):
    if not data:
        logging.info("Нет валидных данных для загрузки в БД.")
        return

    logging.info("Начало заполнения базы данных...")
    try:
        cursor = conn.cursor()
        values_list = [
            (d["user_id"], d["oauth_consumer_key"], d["lis_result_sourcedid"],
             d["lis_outcome_service_url"], d["is_correct"], d["attempt_type"], d["created_at"])
            for d in data
        ]

        insert_query = """
            INSERT INTO grader_stats 
            (user_id, oauth_consumer_key, lis_result_sourcedid, lis_outcome_service_url, is_correct, attempt_type, created_at)
            VALUES %s
            ON CONFLICT DO NOTHING;
        """
        execute_values(cursor, insert_query, values_list)
        conn.commit()
        logging.info(f"Заполнение базы завершено. Успешно загружено записей: {len(values_list)}")
    except Exception as e:
        conn.rollback()
        logging.error(f"Ошибка при загрузке в БД: {e}", exc_info=True)


# ==============================================================================
# 5. АГРЕГАЦИЯ И ВЫГРУЗКА В GOOGLE SHEETS (Задание со звездочкой №1)
# ==============================================================================
def export_to_google_sheets(data: List[Dict[str, Any]]):
    if not data:
        logging.warning("Нет данных для агрегации и выгрузки в Google Sheets.")
        return

    logging.info("Начало агрегации данных и выгрузки в Google Sheets...")
    try:
        # 1. Агрегация данных по дням
        daily_stats = defaultdict(lambda: {"total": 0, "successful": 0, "users": set()})

        for row in data:
            if row["created_at"]:
                date_str = row["created_at"].date().isoformat()
            else:
                date_str = "Unknown_Date"

            daily_stats[date_str]["total"] += 1
            if row["is_correct"] is True:
                daily_stats[date_str]["successful"] += 1
            daily_stats[date_str]["users"].add(row["user_id"])

        # 2. Формирование строк для выгрузки
        rows_to_append = []
        for date_str in sorted(daily_stats.keys()):
            stats = daily_stats[date_str]
            total = stats["total"]
            successful = stats["successful"]
            unique_users = len(stats["users"])
            success_rate = (successful / total * 100) if total > 0 else 0.0

            rows_to_append.append([
                date_str,
                total,
                successful,
                unique_users,
                f"{success_rate:.2f}%"
            ])

        # 3. Аутентификация и подключение к Google Sheets
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(CONFIG["gs_credentials_file"], scopes=scope)
        client = gspread.authorize(creds)

        sheet = client.open_by_url(CONFIG["gs_spreadsheet_url"]).sheet1

        # 4. Проверка и установка заголовков (если таблица пустая)
        if sheet.get_all_values() == []:
            headers = ["Дата", "Всего попыток", "Успешных попыток", "Уникальных пользователей", "Success Rate (%)"]
            sheet.append_row(headers)
            logging.info("Созданы заголовки в Google Таблице.")

        # 5. Пакетная выгрузка данных (намного быстрее, чем append_row в цикле)
        if rows_to_append:
            # Определяем следующую пустую строку
            next_row = len(sheet.get_all_values()) + 1
            range_name = f"A{next_row}:E{next_row + len(rows_to_append) - 1}"
            sheet.update(range_name, rows_to_append)

        logging.info(f"Данные успешно агрегированы и добавлены в Google Sheets ({len(rows_to_append)} строк).")

    except FileNotFoundError:
        logging.error("Файл 'credentials.json' не найден. Проверьте путь и наличие файла.")
    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(
            "Таблица не найдена. Убедитесь, что вы дали доступ (Editor) email-у из credentials.json сервисному аккаунту!")
    except Exception as e:
        logging.error(f"Ошибка при работе с Google Sheets: {e}", exc_info=True)


# ==============================================================================
# ГЛАВНЫЙ БЛОК ВЫПОЛНЕНИЯ
# ==============================================================================
if __name__ == "__main__":
    setup_logging()
    logging.info("=== Запуск скрипта интеграции данных (с Google Sheets) ===")

    conn = None
    try:
        conn = init_db()
        parsed_data = fetch_and_parse_data()
        load_to_db(conn, parsed_data)
        export_to_google_sheets(parsed_data)  # <-- Новая функция
        logging.info("=== Скрипт успешно завершил работу ===")
    except Exception as e:
        logging.critical(f"Критическая ошибка выполнения скрипта: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
            logging.info("Соединение с базой данных закрыто.")