# -*- coding: utf-8 -*-
"""Модуль для управления конфигурацией (config.ini)."""

import configparser
import os
import json
import logging
# Импортируем logger_setup, но будем использовать его осторожно
from . import logger_setup

# Имя файла конфигурации по умолчанию
CONFIG_FILE = "config.ini"

# Значения по умолчанию для настроек
DEFAULT_SETTINGS = {
    "api_url": "https://openrouter.ai/api/v1/chat/completions",
    "host": "127.0.0.1",
    "port": 5000,
    "request_timeout": 30,
    "keys_file": "keys.csv",
    "log_file": "proxy.log",
    "auto_reload_interval": 300,
    "key_recovery_interval": 3600,
    "max_retries": 10,
    "log_level": "INFO",
    "enable_fallback": False,
    "fallback_models": ["openai/gpt-3.5-turbo", "anthropic/claude-3-haiku", "meta-llama/llama-3"],
    # --- Google Sheet Sync Defaults ---
    "enable_google_sheet_sync": False,
    "google_sheet_export_url": "", # Пусто по умолчанию, пользователь должен указать
    "google_sheet_key_column_index": 2, # По умолчанию столбец A
    "google_sheet_sync_interval": 600 # По умолчанию 10 минут
}

# Убираем глобальный объект ConfigParser
# config = configparser.ConfigParser()

def _get_setting_from_parser(config_parser: configparser.ConfigParser, key: str, default_value=None, type_func=str):
    """
    Вспомогательная функция: получает значение настройки из переданного парсера.

    Args:
        config_parser: Экземпляр ConfigParser с загруженными данными.
        key (str): Ключ настройки.
        default_value: Значение по умолчанию, если ключ отсутствует.
        type_func (type): Функция для преобразования типа (int, float, bool, str, json.loads).

    Returns:
        Значение настройки или default_value.
    """
    if default_value is None and key in DEFAULT_SETTINGS:
        default_value = DEFAULT_SETTINGS[key]

    # Используем переданный config_parser
    if not config_parser.has_section("Settings") or not config_parser.has_option("Settings", key):
        # Логирование может еще не быть настроено на этом этапе, поэтому используем стандартный logging
        logging.warning(f"Ключ '{key}' не найден в config.ini. Используется значение по умолчанию: {default_value}")
        return default_value

    value_str = config_parser.get("Settings", key) # Используем переданный config_parser

    try:
        if type_func == bool:
            # configparser.getboolean обрабатывает 'yes'/'no', 'true'/'false', '1'/'0'
            return config_parser.getboolean("Settings", key) # Используем переданный config_parser
        elif type_func == json.loads:
            return json.loads(value_str)
        else:
            # Для int, float, str
            return type_func(value_str)
    except (ValueError, json.JSONDecodeError) as e:
        # Используем стандартный logging для ошибок преобразования
        logging.error(f"Ошибка преобразования значения для ключа '{key}': '{value_str}'. Используется значение по умолчанию. Ошибка: {e}")
        return default_value

def load_config(file_path=CONFIG_FILE):
    """
    Загружает конфигурацию из указанного файла.

    Args:
        file_path (str): Путь к файлу config.ini.

    Returns:
        dict: Словарь с загруженными настройками.
    """
    # Создаем локальный экземпляр ConfigParser для каждого вызова
    config_parser = configparser.ConfigParser()
    if not os.path.exists(file_path):
        # Используем стандартный logging, т.к. logger_setup еще не настроен
        logging.warning(f"[ConfigManager] Файл конфигурации '{file_path}' не найден. Создается файл с настройками по умолчанию.")
        create_default_config(file_path)
    else:
        # Добавляем логирование перед попыткой чтения
        logging.info(f"[ConfigManager] Найден файл конфигурации: '{file_path}'. Попытка чтения...")

    read_ok = False
    try:
        # Читаем файл в локальный парсер
        read_files = config_parser.read(file_path, encoding='utf-8')
        if file_path in read_files:
            read_ok = True
            # Используем стандартный logging для информации о загрузке
            logging.info(f"[ConfigManager] Конфигурация успешно прочитана из '{file_path}'.")
            # Дополнительно логируем прочитанные секции для диагностики
            logging.debug(f"[ConfigManager] Прочитанные секции: {config_parser.sections()}")
            if "Settings" in config_parser:
                logging.debug(f"[ConfigManager] Опции в секции [Settings]: {list(config_parser['Settings'].keys())}")
            else:
                logging.warning("[ConfigManager] Секция [Settings] не найдена в файле конфигурации!")
        else:
            # Файл существует, но не прочитался (пустой или ошибка формата?)
            logging.error(f"[ConfigManager] Не удалось прочитать файл конфигурации '{file_path}', хотя он существует. Проверьте формат и права доступа.")

    except configparser.Error as e:
        # Используем стандартный logging для ошибок чтения
        logging.error(f"[ConfigManager] Ошибка парсинга файла конфигурации '{file_path}': {e}. Будут использованы значения по умолчанию.")
        # В случае ошибки чтения, read_ok будет False
    except Exception as e:
        # Логируем непредвиденные ошибки при чтении
        logging.exception(f"[ConfigManager] Непредвиденная ошибка при чтении файла конфигурации '{file_path}': {e}")

    # Возвращаем словарь всех настроек для удобства
    # Используем _get_setting_from_parser с локальным config_parser
    settings_dict = {}
    for key, default_value in DEFAULT_SETTINGS.items():
        type_func = type(default_value)
        if isinstance(default_value, bool):
            type_func = bool
        elif isinstance(default_value, list) or isinstance(default_value, dict):
             type_func = json.loads # Для списков/словарей используем json

        # Передаем локальный config_parser в _get_setting_from_parser
        settings_dict[key] = _get_setting_from_parser(config_parser, key, default_value, type_func)

    return settings_dict


def create_default_config(file_path=CONFIG_FILE):
    """
    Создает файл config.ini с настройками по умолчанию, если он отсутствует.

    Args:
        file_path (str): Путь для создания файла config.ini.
    """
    if os.path.exists(file_path):
        # Используем стандартный logging
        logging.info(f"Файл конфигурации '{file_path}' уже существует.")
        return

    config_to_write = configparser.ConfigParser()
    config_to_write["Settings"] = {}
    for key, value in DEFAULT_SETTINGS.items():
        if isinstance(value, list) or isinstance(value, dict):
            config_to_write["Settings"][key] = json.dumps(value, ensure_ascii=False)
        else:
            config_to_write["Settings"][key] = str(value)

    try:
        with open(file_path, "w", encoding='utf-8') as f:
            config_to_write.write(f)
        # Используем стандартный logging
        logging.info(f"Создан файл конфигурации по умолчанию: '{file_path}'.")
    except IOError as e:
        # Используем стандартный logging
        logging.error(f"Не удалось создать файл конфигурации '{file_path}': {e}")
