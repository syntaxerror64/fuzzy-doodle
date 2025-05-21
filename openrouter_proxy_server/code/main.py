# main.py
# -*- coding: utf-8 -*-
"""Основной файл запуска прокси-сервера OpenRouter с FastAPI и Uvicorn."""

import asyncio
import sys
import uvicorn
import aiohttp
import logging
import threading # Исправлено: Возвращен импорт threading
from contextlib import asynccontextmanager
from typing import Any # Добавлен импорт Any

from fastapi import FastAPI

# Импортируем модули проекта
from utils_proxy_open import (
    config_manager,
    logger_setup, # Импортируем модуль логгера
    key_manager,
    background_tasks,
    menu as menu_module # Исправлено: Возвращен импорт меню
)
# Импортируем классы напрямую для type hinting и создания экземпляров
from utils_proxy_open.key_manager import KeyManager
from utils_proxy_open.logger_setup import LoggerSetup
from utils_proxy_open.background_tasks import AsyncBackgroundTasks
# Импортируем функцию настройки маршрутов
from utils_proxy_open.proxy_handler import setup_proxy_routes

# --- Lifespan Context Manager ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Код запуска ---
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    app.state.startup_error = None
    logger_instance = None # Определяем для более широкой области видимости

    try:
        # 1. Загрузка конфигурации
        settings = await asyncio.to_thread(config_manager.load_config)
        app.state.settings = settings
        app.state.config_manager = config_manager # Сохраняем модуль
        logging.info("Конфигурация загружена.")

        # 2. Настройка основного логгера
        logger_instance = logger_setup.logger
        logger_instance.setup_logging(
            file_path=settings.get("log_file", config_manager.DEFAULT_SETTINGS["log_file"]),
            level=settings.get("log_level", config_manager.DEFAULT_SETTINGS["log_level"])
        )
        app.state.logger = logger_instance
        logger_instance.log_info("Основной логгер настроен.")

        # 3. Инициализация HTTP сессии
        app.state.http_session = aiohttp.ClientSession()
        logger_instance.log_info("aiohttp.ClientSession инициализирована.")

        # 4. Инициализация KeyManager
        key_manager_instance = KeyManager(
            keys_file_path=settings.get("keys_file", config_manager.DEFAULT_SETTINGS["keys_file"]),
            api_url=settings.get("api_url", config_manager.DEFAULT_SETTINGS["api_url"]),
            validation_timeout=settings.get("request_timeout", config_manager.DEFAULT_SETTINGS["request_timeout"])
        )
        await key_manager_instance.load_keys(
            session=app.state.http_session,
            validate_on_load=settings.get("validate_keys_on_start", False)
        )
        app.state.key_manager_instance = key_manager_instance
        logger_instance.log_info("KeyManager инициализирован и ключи загружены.")

        # 5. Инициализация BackgroundTasks runner
        app.state.background_tasks_runner = AsyncBackgroundTasks()
        logger_instance.log_info("AsyncBackgroundTasks инициализирован.")

        # 6. Запуск фоновых задач
        await app.state.background_tasks_runner.start_background_tasks_async(app, app.state.http_session)
        logger_instance.log_info("Фоновые задачи запущены.")

        # 7. Инициализация и запуск меню в отдельном потоке
        # Исправлено: Восстановлен запуск потока меню.
        app.state.menu_stop_event = threading.Event()
        # Получаем текущий цикл событий asyncio
        loop = asyncio.get_running_loop()
        menu_instance = menu_module.init_menu(loop, app) # Передаем loop и app
        app.state.menu_instance = menu_instance # Сохраняем экземпляр меню

        # Создаем и запускаем поток для меню
        menu_thread = threading.Thread(
            target=menu_instance.run_loop,
            args=(app.state.menu_stop_event,), # Передаем событие остановки
            daemon=True # Поток-демон завершится при выходе основного потока
        )
        app.state.menu_thread = menu_thread
        menu_thread.start()
        logger_instance.log_info("Интерактивное меню запущено в отдельном потоке.")

    except Exception as e:
        app.state.startup_error = repr(e)
        # Исправлено: Упрощено логирование ошибки startup для избежания проблем с форматтером.
        # Решение: Используем стандартный logging или logger.exception без 'extra'.
        log_msg = f"КРИТИЧЕСКАЯ ОШИБКА во время startup: {repr(e)}"
        print(f"{log_msg}. Приложение может работать некорректно.", file=sys.stderr)
        if logger_instance and logger_instance.is_setup:
            # Используем logger.exception, который включает traceback
            logger_instance.logger.exception(log_msg)
        else:
            logging.exception(log_msg) # Используем стандартный logging
        # raise # Можно раскомментировать, чтобы остановить запуск Uvicorn

    # --- Приложение работает ---
    yield
    # --- Код завершения ---

    logger = app.state.logger if hasattr(app.state, 'logger') else logging
    logger.log_info("Событие shutdown: Завершение работы...")

    # 1. Остановка меню
    # Исправлено: Восстановлена логика остановки потока меню.
    if hasattr(app.state, 'menu_thread') and app.state.menu_thread.is_alive():
        logger.log_info("Остановка потока меню...")
        if hasattr(app.state, 'menu_stop_event'):
            app.state.menu_stop_event.set() # Сигнализируем меню остановиться
        try:
            # Даем меню немного времени на завершение ввода/вывода
            app.state.menu_thread.join(timeout=2.0)
            if app.state.menu_thread.is_alive():
                 logger.log_warning("Поток меню не завершился за 2 секунды.")
            else:
                 logger.log_info("Поток меню успешно завершен.")
        except Exception as e:
            logger.log_error(f"Ошибка при ожидании завершения потока меню: {repr(e)}", exc_info=True)

    # 2. Остановка фоновых задач
    if hasattr(app.state, 'background_tasks_runner'):
        logger.log_info("Остановка фоновых задач...")
        try:
            await app.state.background_tasks_runner.stop_background_tasks_async()
            logger.log_info("Фоновые задачи остановлены.")
        except Exception as e:
            logger.log_error(f"Ошибка при остановке фоновых задач: {repr(e)}", exc_info=True)

    # 3. Закрытие HTTP сессии
    if hasattr(app.state, 'http_session') and app.state.http_session and not app.state.http_session.closed:
        logger.log_info("Закрытие aiohttp.ClientSession...")
        try:
            await app.state.http_session.close()
            await asyncio.sleep(0.1)
            logger.log_info("aiohttp.ClientSession закрыта.")
        except Exception as e:
            logger.log_error(f"Ошибка при закрытии aiohttp сессии: {repr(e)}", exc_info=True)

    logger.log_info("Завершение работы выполнено.")
    # 4. Закрытие обработчиков логгера
    if hasattr(app.state, 'logger') and app.state.logger.is_setup:
        logger.log_info("Закрытие обработчиков логгера...")
        logger_instance: LoggerSetup = app.state.logger
        actual_logger = logger_instance.logger
        for handler in actual_logger.handlers[:]:
            try:
                 handler.close()
                 actual_logger.removeHandler(handler)
            except Exception as e:
                 logging.error(f"Ошибка при закрытии/удалении обработчика логов: {e!r}")
        logger.log_info("Обработчики логгера закрыты.")

# --- Создание экземпляра FastAPI с lifespan ---
app = FastAPI(
    title="OpenRouter Proxy",
    description="Асинхронный прокси-сервер для OpenRouter API",
    version="1.0.0", # TODO: Реализовать версионирование API в будущем, если потребуется.
    lifespan=lifespan # Используем lifespan context manager
)

# --- Настройка маршрутов ---
setup_proxy_routes(app)

# --- Запуск сервера через Uvicorn ---
if __name__ == '__main__':
    # Загружаем минимальные настройки для uvicorn
    host = '127.0.0.1'
    port = 5000
    log_level_uvicorn = 'info'
    try:
        initial_settings = config_manager.load_config()
        host = initial_settings.get('host', config_manager.DEFAULT_SETTINGS['host'])
        port = initial_settings.get('port', config_manager.DEFAULT_SETTINGS['port'])
        log_level_config = initial_settings.get('log_level', config_manager.DEFAULT_SETTINGS['log_level']).lower()
        if log_level_config in ['critical', 'error', 'warning', 'info', 'debug', 'trace']:
             log_level_uvicorn = log_level_config
        else:
             print(f"Предупреждение: Некорректный log_level '{log_level_config}' в config.ini для Uvicorn. Используется 'info'.", file=sys.stderr)
    except Exception as e:
        print(f"Ошибка при загрузке начальной конфигурации для Uvicorn: {repr(e)}", file=sys.stderr)
        host = config_manager.DEFAULT_SETTINGS['host']
        port = config_manager.DEFAULT_SETTINGS['port']
        log_level_uvicorn = config_manager.DEFAULT_SETTINGS['log_level'].lower()
        print(f"Используются значения по умолчанию: host={host}, port={port}, log_level={log_level_uvicorn}")

    print(f"Запуск FastAPI сервера на http://{host}:{port}...")
    print("Для остановки нажмите Ctrl+C")

    # Исправлено: Используем uvicorn.Server и asyncio.run для запуска.
    config = uvicorn.Config("main:app", host=host, port=port, log_level=log_level_uvicorn, lifespan="on")
    server = uvicorn.Server(config)

    # Запускаем сервер асинхронно
    # Используем asyncio.run() для управления циклом событий
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("\nПолучен сигнал KeyboardInterrupt. Завершение работы...")
        # Lifespan должен автоматически вызваться при остановке server.serve()
    except Exception as e:
        print(f"\nКритическая ошибка при запуске/работе Uvicorn сервера: {repr(e)}", file=sys.stderr)
        # Логируем, если логгер успел инициализироваться
        if hasattr(app, 'state') and hasattr(app.state, 'logger') and app.state.logger.is_setup:
             app.state.logger.log_critical(f"Критическая ошибка Uvicorn сервера: {repr(e)}", exc_info=True)
        sys.exit(1)
    finally:
        # Дополнительная очистка, если нужна, но lifespan должен справиться
        print("Приложение завершено.")
