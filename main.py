import os
import glob
import logging
import requests
import ast
import datetime
import psycopg2
from psycopg2.extras import execute_values

# ==============================================================================
# КОНФИГУРАЦИЯ (ЗАПОЛНИТЕ ДАННЫЕ ПОДКЛЮЧЕНИЯ К ВАШЕЙ ЛОКАЛЬНОЙ PostgreSQL)
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
}


# ==============================================================================
# 1. НАСТРОЙКА ЛОГИРОВАНИЯ И РОТАЦИЯ (Хранение последних 3 дней)
# ==============================================================================
def setup_logging():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    today = datetime.date.today()

    # Удаляем логи старше 3 дней
    for filepath in glob.glob(os.path.join(log_dir, "log_*.txt")):
        try:
            filename = os.path.basename(filepath)
            date_str = filename.replace("log_", "").replace(".txt", "")
            file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()

            if (today - file_date).days > 3:
                os.remove(filepath)
        except Exception:
            pass  # Игнорируем файлы с некорректным именем

    log_file = os.path.join(log_dir, f"log_{today.strftime('%Y-%m-%d')}.txt")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()  # Дублирование в консоль для удобства
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
            host=CONFIG["db_host"],
            port=CONFIG["db_port"],
            dbname=CONFIG["db_name"],
            user=CONFIG["db_user"],
            password=CONFIG["db_password"]
        )
        cursor = conn.cursor()

        create_table_query = """
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
        """
        cursor.execute(create_table_query)
        conn.commit()
        logging.info("Таблица 'grader_stats' успешно проверена/создана.")
        return conn
    except Exception as e:
        logging.error(f"Критическая ошибка при инициализации БД: {e}", exc_info=True)
        raise


# ==============================================================================
# 3. ПОЛУЧЕНИЕ И ВАЛИДАЦИЯ ДАННЫХ ИЗ API
# ==============================================================================
def fetch_and_parse_data() -> list:
    logging.info("Начало скачивания данных с API...")

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
        logging.info(f"Скачивание завершено. Всего получено записей: {len(data)}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка доступа к API (Status Code или соединение): {e}", exc_info=True)
        return []

    parsed_data = []
    for idx, record in enumerate(data):
        try:
            # 1. Парсинг passback_params (строка, похожая на dict)
            passback_str = record.get("passback_params", "{}")
            try:
                # ast.literal_eval безопасно вычисляет строку как Python-объект
                passback_dict = ast.literal_eval(passback_str)
            except (ValueError, SyntaxError):
                passback_dict = {}  # Если строка битая, используем пустой dict

            if not isinstance(passback_dict, dict):
                passback_dict = {}

            # 2. Валидация обязательных полей
            user_id = str(record.get("lti_user_id", "")).strip()
            if not user_id:
                logging.warning(f"Пропуск записи #{idx}: отсутствует или пустой user_id")
                continue

            attempt_type = str(record.get("attempt_type", "")).strip().lower()
            if attempt_type not in ["run", "submit"]:
                logging.warning(f"Пропуск записи #{idx}: некорректный attempt_type '{attempt_type}'")
                continue

            # 3. Приведение типов
            is_correct_raw = record.get("is_correct")
            is_correct = None if is_correct_raw is None else bool(is_correct_raw)

            created_at_str = record.get("created_at")
            try:
                created_at = datetime.datetime.fromisoformat(created_at_str) if created_at_str else None
            except ValueError:
                created_at = None
                logging.warning(f"Пропуск записи #{idx}: некорректный формат даты '{created_at_str}'")
                continue

            # 4. Формирование чистой записи
            parsed_record = {
                "user_id": user_id,
                "oauth_consumer_key": str(passback_dict.get("oauth_consumer_key", "")).strip(),
                "lis_result_sourcedid": str(passback_dict.get("lis_result_sourcedid", "")).strip(),
                "lis_outcome_service_url": str(passback_dict.get("lis_outcome_service_url", "")).strip(),
                "is_correct": is_correct,
                "attempt_type": attempt_type,
                "created_at": created_at
            }
            parsed_data.append(parsed_record)

        except Exception as e:
            logging.error(f"Ошибка парсинга записи #{idx}: {e}. Сырые данные: {record}")

    logging.info(f"Обработка данных завершена. Валидных записей для загрузки: {len(parsed_data)}")
    return parsed_data


# ==============================================================================
# 4. ЗАГРУЗКА ДАННЫХ В БАЗУ (Batch Insert для производительности)
# ==============================================================================
def load_to_db(conn, data: list):
    if not data:
        logging.info("Нет валидных данных для загрузки в БД. Завершение.")
        return

    logging.info("Начало заполнения базы данных...")
    try:
        cursor = conn.cursor()

        # Подготовка кортежей для массовой вставки
        values_list = [
            (
                d["user_id"], d["oauth_consumer_key"], d["lis_result_sourcedid"],
                d["lis_outcome_service_url"], d["is_correct"], d["attempt_type"], d["created_at"]
            )
            for d in data
        ]

        insert_query = """
        INSERT INTO grader_stats 
        (user_id, oauth_consumer_key, lis_result_sourcedid, lis_outcome_service_url, is_correct, attempt_type, created_at)
        VALUES %s
        ON CONFLICT DO NOTHING; -- Защита от дубликатов при повторном запуске
        """

        # execute_values намного быстрее циклического INSERT
        execute_values(cursor, insert_query, values_list)
        conn.commit()
        logging.info(f"Заполнение базы завершено. Успешно загружено записей: {len(values_list)}")
    except Exception as e:
        conn.rollback()
        logging.error(f"Ошибка при загрузке в БД, выполнен откат транзакции: {e}", exc_info=True)


# ==============================================================================
# ГЛАВНЫЙ БЛОК ВЫПОЛНЕНИЯ
# ==============================================================================
if __name__ == "__main__":
    setup_logging()
    logging.info("=== Запуск основного скрипта интеграции данных ===")

    conn = None
    try:
        conn = init_db()
        parsed_data = fetch_and_parse_data()
        load_to_db(conn, parsed_data)
        logging.info("=== Скрипт успешно завершил работу ===")
    except Exception as e:
        logging.critical(f"Критическая ошибка выполнения скрипта: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
            logging.info("Соединение с базой данных закрыто.")