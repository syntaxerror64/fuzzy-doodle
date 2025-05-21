# utils_proxy_open/background_tasks.py
# -*- coding: utf-8 -*-
"""Асинхронный модуль для фоновых задач (восстановление ключей, перезагрузка конфига, сохранение ключей, синхронизация GSheet)."""

import asyncio
import csv
import io # Для обработки строк как файлов
import logging
import sys
from typing import Dict, Optional, Set, List

import aiohttp
from fastapi import FastAPI

# Исправлено: Импортируем классы напрямую для type hinting, но ConfigManager - как модуль
from .key_manager import KeyManager
from . import config_manager # Импортируем как модуль
from . import logger_setup

# Интервал проверки необходимости сохранения ключей (в секундах)
SAVE_CHECK_INTERVAL = 10

# Исправлено: Удалена старая функция sync_keys_from_google_sheet_async.
# Решение: Логика перенесена в KeyManager.sync_keys_from_google_sheet.

# --- Класс для управления асинхронными фоновыми задачами ---

class AsyncBackgroundTasks:
    # Исправлено: Конструктор изменен для получения app и инициализации менеджеров позже.
    # Решение: Менеджеры (KeyManager, ConfigManager) будут получены из app.state при запуске задач.
    # Исправлено: Тип self.config_manager изменен на модуль.
    def __init__(self):
        self.key_manager: Optional[KeyManager] = None # Инициализируем как None
        self.config_manager = None # Инициализируем как None (будет модуль)
        # self.current_config_data: Dict = {} # Больше не храним конфиг здесь, берем из ConfigManager
        self.tasks: List[asyncio.Task] = []
        self.stop_event = asyncio.Event()
        # self.config_lock = asyncio.Lock() # Блокировка больше не нужна здесь
        self._running = False
        self.session: Optional[aiohttp.ClientSession] = None
        self.app: Optional[FastAPI] = None # Для доступа к app.state
        logger_setup.logger.log_info("AsyncBackgroundTasks инициализирован (менеджеры будут установлены при запуске).")

    def is_running(self) -> bool:
        """Проверяет, запущены ли фоновые задачи."""
        # Проверяем флаг и наличие активных (не завершенных) задач
        # Проверяем флаг и наличие активных (не завершенных) задач
        return self._running and any(task and not task.done() for task in self.tasks)

    # Исправлено: Метод запуска задач изменен для получения менеджеров из app.state.
    # Решение: KeyManager и ConfigManager извлекаются из app.state.
    async def start_background_tasks_async(self, app: FastAPI, session: aiohttp.ClientSession):
        """Асинхронно запускает фоновые задачи, получая менеджеры из app.state."""
        if self._running:
            logger_setup.logger.log_warning("Попытка повторного запуска фоновых задач.")
            return

        self.app = app # Сохраняем app
        self.session = session # Сохраняем сессию

        # Проверяем сессию
        if not self.session or self.session.closed:
             logger_setup.logger.log_error("Передана невалидная сессия aiohttp. Невозможно запустить фоновые задачи.")
             return

        # Получаем менеджеры из app.state
        try:
            # Исправлено: Получаем модуль config_manager из app.state.
            # Решение: Проверяем наличие атрибута, но не тип ConfigManager.
            self.key_manager = app.state.key_manager_instance # Используем правильное имя из main.py
            self.config_manager = app.state.config_manager # Получаем модуль
            if not isinstance(self.key_manager, KeyManager):
                 raise AttributeError("app.state.key_manager_instance не является экземпляром KeyManager")
            if not hasattr(app.state, 'config_manager'): # Проверяем наличие модуля
                 raise AttributeError("Модуль config_manager не найден в app.state")
        except AttributeError as e:
            logger_setup.logger.log_error(f"Не удалось получить менеджеры из app.state: {e}. Фоновые задачи не запущены.")
            self.app = None # Сбрасываем, если не удалось
            self.session = None
            return

        self._running = True
        self.stop_event.clear()
        self.tasks = [] # Очищаем список перед стартом

        logger_setup.logger.log_info("Запуск асинхронных фоновых задач...")

        # Задача восстановления ключей
        self.tasks.append(asyncio.create_task(self._run_key_recovery(), name="KeyRecoveryTask"))

        # Задача перезагрузки конфига
        self.tasks.append(asyncio.create_task(self._run_config_reload(), name="ConfigReloadTask"))

        # Задача периодического сохранения ключей
        self.tasks.append(asyncio.create_task(self._run_save_keys(), name="KeySaveTask"))

        # Задача синхронизации с Google Sheets
        self.tasks.append(asyncio.create_task(self._run_google_sheet_sync_loop(), name="GoogleSheetSyncTask"))

        logger_setup.logger.log_info(f"Асинхронные фоновые задачи ({len(self.tasks)}) созданы.")

    async def stop_background_tasks_async(self):
        """Асинхронно останавливает все фоновые задачи."""
        if not self._running:
            return

        logger_setup.logger.log_info("Остановка асинхронных фоновых задач...")
        self.stop_event.set() # Сигнализируем задачам остановиться

        # Отменяем задачи
        cancelled_tasks = []
        for task in self.tasks:
            if task and not task.done(): # Добавили проверку task
                task.cancel()
                cancelled_tasks.append(task)

        # Ожидаем завершения отмененных задач
        if cancelled_tasks:
            logger_setup.logger.log_debug(f"Ожидание завершения {len(cancelled_tasks)} задач...")
            results = await asyncio.gather(*cancelled_tasks, return_exceptions=True)
            for i, result in enumerate(results):
                 if isinstance(result, asyncio.CancelledError):
                      logger_setup.logger.log_debug(f"Задача '{cancelled_tasks[i].get_name()}' успешно отменена.")
                 elif isinstance(result, Exception):
                      logger_setup.logger.log_error(f"Ошибка при отмене задачи '{cancelled_tasks[i].get_name()}': {result!r}")
            logger_setup.logger.log_debug(f"Завершено {len(cancelled_tasks)} отмененных задач.")


        self.tasks.clear()
        self._running = False
        # Сессию не закрываем здесь, т.к. она управляется извне (в main.py shutdown_event)
        # Сбрасываем ссылки на менеджеры и сессию
        self.key_manager = None
        self.config_manager = None
        self.session = None
        self.app = None
        logger_setup.logger.log_info("Асинхронные фоновые задачи остановлены.")

    # Исправлено: Удален аргумент session, используется self.session. Добавлены проверки менеджеров.
    async def _run_key_recovery(self):
        """Асинхронная фоновая задача: восстановление невалидных ключей."""
        logger_setup.logger.log_info("Задача восстановления ключей запущена.")
        while not self.stop_event.is_set():
            recovery_interval = 3600 # Значение по умолчанию
            try:
                # Проверяем наличие менеджеров и сессии
                if not self.key_manager or not self.config_manager or not self.session or self.session.closed:
                    logger_setup.logger.log_warning("KeyManager, ConfigManager или сессия недоступны в _run_key_recovery. Пропуск итерации.")
                    await asyncio.sleep(SAVE_CHECK_INTERVAL) # Ждем немного перед следующей попыткой
                    continue

                # Исправлено: Получаем настройки из словаря, возвращаемого config_manager.load_config().
                # Решение: Загружаем настройки и получаем значение по ключу.
                # Загружаем актуальные настройки (может быть неэффективно, лучше передавать)
                # Лучше получать настройки из app.state, если они там обновляются
                current_settings = await asyncio.to_thread(self.config_manager.load_config)
                recovery_interval = current_settings.get("key_recovery_interval", 3600)

                logger_setup.logger.log_debug(f"Проверка невалидных ключей (интервал {recovery_interval} сек)...")

                # Вызываем метод экземпляра key_manager, используя self.session
                recovered = await self.key_manager.recover_bad_keys(session=self.session)
                # Логирование теперь внутри recover_bad_keys

            except asyncio.CancelledError:
                logger_setup.logger.log_info("Задача восстановления ключей отменена.")
                break
            except Exception as e:
                logger_setup.logger.log_exception("Ошибка в задаче восстановления ключей:")
                # Продолжаем работу после ошибки, но ждем интервал

            try:
                 # Используем asyncio.wait_for для ожидания с таймаутом
                 await asyncio.wait_for(self.stop_event.wait(), timeout=recovery_interval)
            except asyncio.TimeoutError:
                 continue # Таймаут истек, продолжаем цикл
            except asyncio.CancelledError:
                 logger_setup.logger.log_info("Ожидание в задаче восстановления ключей отменено.")
                 break # Выходим из цикла при отмене
        logger_setup.logger.log_info("Задача восстановления ключей завершена.")

    # Исправлено: Используется self.config_manager для получения интервала и перезагрузки.
    async def _run_config_reload(self):
        """Асинхронная фоновая задача: перезагрузка конфигурации."""
        logger_setup.logger.log_info("Задача перезагрузки конфига запущена.")
        while not self.stop_event.is_set():
            reload_interval = 300 # Значение по умолчанию
            try:
                # Проверяем наличие ConfigManager
                if not self.config_manager:
                    logger_setup.logger.log_warning("ConfigManager недоступен в _run_config_reload. Пропуск итерации.")
                    await asyncio.sleep(SAVE_CHECK_INTERVAL)
                    continue

                # Исправлено: Получаем настройки из словаря.
                # Решение: Загружаем настройки и получаем значения по ключам.
                current_settings = await asyncio.to_thread(self.config_manager.load_config)
                reload_interval = current_settings.get("auto_reload_interval", 300)
                config_file = self.config_manager.CONFIG_FILE # Используем константу из модуля

                logger_setup.logger.log_debug(f"Проверка обновления конфига '{config_file}' (интервал {reload_interval} сек)...")

                # Перезагружаем конфиг и получаем новый словарь настроек
                new_settings = await asyncio.to_thread(self.config_manager.load_config)

                # Сравниваем со старыми настройками (нужно их где-то хранить или передавать)
                # Простой вариант: всегда обновляем параметры KeyManager
                if self.key_manager:
                    # logger_setup.logger.log_info("Обновляем параметры KeyManager после проверки конфига...")
                    self.key_manager.api_url = new_settings.get("api_url", self.config_manager.DEFAULT_SETTINGS["api_url"])
                    self.key_manager.validation_timeout = new_settings.get("request_timeout", self.config_manager.DEFAULT_SETTINGS["request_timeout"])
                # Обновляем настройки в app.state, если они там используются напрямую
                if self.app and hasattr(self.app.state, 'settings'):
                     # Сравним, чтобы не обновлять без надобности
                     if self.app.state.settings != new_settings:
                          self.app.state.settings = new_settings.copy()
                          logger_setup.logger.log_info("Настройки в app.state обновлены после перезагрузки конфига.")


            except asyncio.CancelledError:
                logger_setup.logger.log_info("Задача перезагрузки конфига отменена.")
                break
            except Exception as e:
                logger_setup.logger.log_exception("Ошибка при автоматической перезагрузке конфигурации:")

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=reload_interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                 logger_setup.logger.log_info("Ожидание в задаче перезагрузки конфига отменено.")
                 break
        logger_setup.logger.log_info("Задача перезагрузки конфига завершена.")

    # Исправлено: Переименовано для единообразия, добавлена проверка self.key_manager.
    async def _run_save_keys(self):
        """Асинхронная фоновая задача: периодическое сохранение ключей, если есть изменения."""
        logger_setup.logger.log_info("Задача сохранения ключей запущена.")
        while not self.stop_event.is_set():
            try:
                # Проверяем наличие KeyManager
                if not self.key_manager:
                    logger_setup.logger.log_warning("KeyManager недоступен в _run_save_keys. Пропуск итерации.")
                    await asyncio.sleep(SAVE_CHECK_INTERVAL)
                    continue

                # Вызываем метод экземпляра key_manager
                await self.key_manager.save_keys()

            except asyncio.CancelledError:
                logger_setup.logger.log_info("Задача сохранения ключей отменена.")
                break
            except Exception as e:
                 logger_setup.logger.log_exception("Ошибка в задаче сохранения ключей:")

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=SAVE_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                 logger_setup.logger.log_info("Ожидание в задаче сохранения ключей отменено.")
                 break
        logger_setup.logger.log_info("Задача сохранения ключей завершена.")

    # Исправлено: Удалены методы get_current_config и update_config, т.к. конфиг управляется ConfigManager.
    # Решение: Задачи теперь напрямую используют self.config_manager.

    # Исправлено: Используется self.key_manager.sync_keys_from_google_sheet. Удален аргумент session.
    async def trigger_google_sheet_sync_now(self):
        """Асинхронно выполняет синхронизацию с Google Sheets немедленно."""
        logger_setup.logger.log_info("Запрошена ручная асинхронная синхронизация с Google Sheets...")
        # Проверяем наличие менеджеров и сессии
        if not self.key_manager or not self.config_manager or not self.session or self.session.closed:
            logger_setup.logger.log_error("KeyManager, ConfigManager или сессия недоступны для ручной синхронизации GSheet.")
            return False # Возвращаем False в случае ошибки

        try:
            # Исправлено: Передаем словарь настроек в sync_keys_from_google_sheet.
            # Решение: Загружаем актуальные настройки и передаем их.
            current_settings = await asyncio.to_thread(self.config_manager.load_config)
            # Вызываем метод экземпляра key_manager
            sync_result = await self.key_manager.sync_keys_from_google_sheet(
                session=self.session,
                settings=current_settings # Передаем словарь настроек
            )
            logger_setup.logger.log_info(f"Ручная синхронизация завершена. Были ли изменения: {sync_result}")
            return sync_result # Возвращаем результат (True если были изменения, False если нет)
        except Exception as e:
            logger_setup.logger.log_exception("Ошибка при ручной синхронизации Google Sheets:")
            return False

    # Исправлено: Используется self.key_manager.sync_keys_from_google_sheet. Удален аргумент session.
    async def _run_google_sheet_sync_loop(self):
        """Асинхронный цикл фоновой задачи для периодической синхронизации с Google Sheets."""
        logger_setup.logger.log_info("Цикл синхронизации Google Sheets запущен.")
        while not self.stop_event.is_set():
            sync_interval = 600 # Значение по умолчанию
            try:
                # Проверяем наличие менеджеров и сессии
                if not self.key_manager or not self.config_manager or not self.session or self.session.closed:
                    logger_setup.logger.log_warning("KeyManager, ConfigManager или сессия недоступны в _run_google_sheet_sync_loop. Пропуск итерации.")
                    await asyncio.sleep(SAVE_CHECK_INTERVAL)
                    continue

                # Исправлено: Получаем настройки из словаря.
                # Решение: Загружаем настройки и получаем значения по ключам.
                current_settings = await asyncio.to_thread(self.config_manager.load_config)
                sync_interval = current_settings.get("google_sheet_sync_interval", 600)
                sync_enabled = current_settings.get("enable_google_sheet_sync", False)

                # Проверяем, включена ли синхронизация
                if not sync_enabled:
                    logger_setup.logger.log_debug("Автоматическая синхронизация Google Sheets отключена в конфиге. Пропуск итерации.")
                    # Ждем интервал перед следующей проверкой
                    await asyncio.wait_for(self.stop_event.wait(), timeout=sync_interval)
                    continue

                # Выполняем синхронизацию, передавая актуальные настройки
                await self.key_manager.sync_keys_from_google_sheet(
                    session=self.session,
                    settings=current_settings
                )

            except asyncio.CancelledError:
                logger_setup.logger.log_info("Цикл синхронизации Google Sheets отменен.")
                break
            except Exception as e:
                # Логируем ошибку, но продолжаем цикл
                logger_setup.logger.log_exception("Ошибка в цикле синхронизации Google Sheets:")

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=sync_interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                 logger_setup.logger.log_info("Ожидание в цикле синхронизации Google Sheets отменено.")
                 break
        logger_setup.logger.log_info("Цикл синхронизации Google Sheets завершен.")
