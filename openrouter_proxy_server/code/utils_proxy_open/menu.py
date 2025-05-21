# utils_proxy_open/menu.py
# -*- coding: utf-8 -*-
"""Модуль для реализации интерактивного меню (адаптирован для async)."""

import time
import threading
import asyncio
import functools # Для partial
import sys
from typing import Optional, Coroutine, Any, List, TYPE_CHECKING

# Используем TYPE_CHECKING для импортов только для type hinting
if TYPE_CHECKING:
    from .key_manager import KeyManager
    from .config_manager import ConfigManager
    from .logger_setup import LoggerSetup
    from .background_tasks import AsyncBackgroundTasks
    import aiohttp

# Импортируем FastAPI для type hinting
from fastapi import FastAPI
# Импортируем logger напрямую для логирования ошибок в меню
from .logger_setup import logger as menu_logger

class Menu:
    def __init__(self, loop: asyncio.AbstractEventLoop, app: FastAPI):
        # Исправлено: Инициализация без lock, т.к. синхронизация через event loop.
        # Решение: Убран self.lock.
        self.running = False
        self.status = "остановлен"
        self.loop = loop # Сохраняем цикл событий
        self.app = app   # Сохраняем объект FastAPI приложения

    def start(self):
        """Отмечает меню как запущенное."""
        self.running = True
        self.status = "работает"
        return self

    def stop(self):
        """Останавливает меню."""
        # Не нужно блокировать здесь, т.к. running проверяется в цикле
        self.running = False
        self.status = "остановлен"

    def _run_async_and_get_result(self, coro: Coroutine) -> Any:
        """
        Безопасно запускает корутину в цикле событий из потока меню и возвращает результат.
        Блокирует поток меню до завершения корутины.
        """
        if not self.loop or self.loop.is_closed():
            print("\nОшибка: Цикл событий asyncio не доступен или закрыт.")
            return None
        try:
            # Запускаем корутину в цикле событий и ждем результат
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            # Ожидаем результат (блокирует текущий поток)
            # Добавляем таймаут на всякий случай
            return future.result(timeout=60) # Таймаут 60 секунд
        except asyncio.TimeoutError:
             print("\nОшибка: Таймаут при ожидании результата асинхронной операции.")
             # Используем импортированный логгер
             menu_logger.log_error("Таймаут в _run_async_and_get_result")
             return None
        except Exception as e:
            print(f"\nОшибка выполнения асинхронной операции: {repr(e)}")
            # Логируем ошибку
            try:
                # Используем импортированный логгер
                coro_name = getattr(coro, '__name__', repr(coro))
                log_msg = f"Ошибка в _run_async_and_get_result при выполнении {coro_name}: {repr(e)}"
                # Запускаем логирование в цикле событий
                self.loop.call_soon_threadsafe(menu_logger.log_error, log_msg, exc_info=True)
            except Exception as log_e:
                 print(f"Дополнительная ошибка при логировании: {log_e}")
            return None

    # Восстановлен оригинальный цикл меню с input()
    # Добавлена пауза и улучшен вывод для ясности
    def run_loop(self, stop_event: Optional[threading.Event] = None):
        """
        Запускает основной цикл обработки меню в текущем потоке.
        Использует stop_event для внешней остановки.
        """
        self.start() # Отмечаем меню как запущенное
        try:
            while self.running:
                # Небольшая пауза перед показом меню, чтобы логи успели вывестись
                if stop_event:
                     # Ждем события или таймаута 1 сек
                     stop_event.wait(timeout=1.0)
                     if stop_event.is_set():
                          print("\n[Меню] Получен внешний сигнал остановки...")
                          break
                else:
                     time.sleep(1.0) # Просто спим, если нет события

                # Проверяем self.running еще раз после паузы
                if not self.running:
                     break

                self._show_menu() # Показываем меню перед input
                choice = None # Инициализируем choice
                try:
                    choice = input("\nВыберите пункт меню: ")
                except EOFError: # Если stdin закрыт
                     print("\nОшибка ввода (EOF). Завершение работы меню...")
                     self.stop() # Устанавливаем флаг остановки
                     break # Выходим из цикла run_loop
                except KeyboardInterrupt: # Ловим Ctrl+C здесь
                     print("\nПолучено прерывание (Ctrl+C) в меню...")
                     # Логируем событие выхода по CTRL+C
                     try:
                         menu_logger.log_warning("[EXIT] Admin press CTRL+C in cosole [/EXIT]")
                     except Exception as log_e:
                         print(f"Ошибка логирования CTRL+C: {log_e}")
                     # Не выходим из цикла здесь, даем главному потоку обработать Ctrl+C
                     # и установить stop_event
                     continue # Просто ждем следующей итерации или stop_event

                # Проверяем снова после input, т.к. событие могло установиться во время ожидания
                if stop_event and stop_event.is_set():
                    break

                # Обрабатываем выбор только если он был получен
                if choice is not None:
                    self._handle_choice(choice)

        except Exception as e:
            print(f"\nКритическая ошибка в цикле меню: {e}")
            try:
                # Используем импортированный логгер
                menu_logger.log_exception(f"Критическая ошибка в цикле меню: {e}")
            except Exception: pass # Игнорируем ошибки логирования здесь
        finally:
            self.stop() # Устанавливает self.running = False, если еще не установлено
            print("\n[INFO] Поток меню завершен.")
            menu_logger.log_info("Поток меню завершен.")

    # Исправлено: Получение статуса фоновых задач из app.state.
    # Решение: Используется self.app.state.background_tasks_runner.is_running().
    # Добавлены разделители для лучшей читаемости.
    def _show_menu(self):
        """Отображает главное меню."""
        # self._clear_screen() # Убираем очистку экрана, т.к. она мешает видеть логи
        print("\n" + "--- МЕНЮ ПРОКСИ-СЕРВЕРА " + "-"*40)

        status_str = "НЕИЗВЕСТНО"
        try:
            if hasattr(self.app.state, 'background_tasks_runner'):
                bg_runner: 'AsyncBackgroundTasks' = self.app.state.background_tasks_runner
                status_str = "РАБОТАЕТ" if bg_runner.is_running() else "ОСТАНОВЛЕН"
            else:
                 status_str = "ЗАПУСК..." # Если runner еще не создан
        except Exception as e:
             status_str = f"ОШИБКА ({e!r})"

        print(f"OpenRouter Proxy [{status_str}]")
        print("="*40 + "\n")

        sections = {
            "1": "Состояние сервера",
            "2": "Статистика",
            "3": "Просмотр логов",
            "4": "Управление ключами",
            "5": "Параметры для подключения",
            "6": "Синхронизировать ключи из Google Sheets",
            "0": "Выход"
        }

        for key, desc in sections.items():
            print(f"[{key}] {desc}")

    def _handle_choice(self, choice):
        """Обрабатывает выбор пользователя."""
        # Не используем lock здесь, т.к. операции теперь могут быть долгими (async)
        if choice == "1":
            self._show_status()
        elif choice == "2":
            self._show_stats()
        elif choice == "3":
            self._show_logs()
        elif choice == "4":
            self._manage_keys()
        elif choice == "5":
            self._show_connection_params()
        elif choice == "6":
            self._manual_google_sheet_sync()
        elif choice == "0":
            self.stop() # Сигнализируем о желании выйти
        else:
            print("\nНеверный выбор.")
            try:
                input("Нажмите Enter для продолжения...") # Пауза при неверном выборе
            except EOFError:
                print("\nОшибка ввода (EOF). Завершение работы меню...")
                self.stop() # Сигнализируем об остановке


    # Исправлено: Получение параметров подключения из ConfigManager в app.state.
    # Решение: Используется self.app.state.config_manager.
    def _show_connection_params(self):
        """Показывает параметры для подключения к прокси."""
        self._clear_screen()
        print("\n" + "="*30 + " ПАРАМЕТРЫ ДЛЯ ПОДКЛЮЧЕНИЯ (OpenAI-Compatible) " + "="*30)

        # Исправлено: Получение host/port из app.state.settings.
        # Решение: Используем self.app.state.settings.get().
        host = "Не удалось получить"
        port = "Не удалось получить"
        try:
            if hasattr(self.app.state, 'settings'):
                settings = self.app.state.settings
                # Импортируем config_manager для доступа к DEFAULT_SETTINGS
                from . import config_manager as cm_module
                host = settings.get("host", cm_module.DEFAULT_SETTINGS['host'])
                port = settings.get("port", cm_module.DEFAULT_SETTINGS['port'])
            else:
                 print("\nОшибка: Настройки (settings) не найдены в app.state.")
        except Exception as e:
             print(f"\nОшибка при получении параметров host/port из настроек: {e!r}")

        base_url = f"http://{host}:{port}"

        print(f"\nBase URL:          {base_url}")
        print(f"API Key:           Любой ключ (не используется прокси, авторизация через keys.csv)")
        print(f"Model ID:          Любая модель, поддерживаемая OpenRouter (указывается в запросе)")
        print(f"\nПолный эндпоинт:   {base_url}/chat/completions")

        print("\n" + "="*70)
        print("Примечание: Этот прокси-сервер использует пул ключей из keys.csv.")
        print("Вам не нужно указывать конкретный API-ключ при подключении вашего софта.")
        print("Просто используйте указанный Base URL и любой Model ID, который вы хотите использовать через OpenRouter.")
        try:
            input("\nНажмите Enter для возврата в главное меню...")
        except EOFError:
            print("\nОшибка ввода (EOF). Завершение работы меню...")
            self.stop()

    # Исправлено: Вызов синхронизации через экземпляр AsyncBackgroundTasks из app.state.
    # Решение: Используется self.app.state.background_tasks_runner.trigger_google_sheet_sync_now().
    def _manual_google_sheet_sync(self):
        """Запускает ручную синхронизацию ключей из Google Sheets."""
        self._clear_screen()
        print("\n" + "="*30 + " СИНХРОНИЗАЦИЯ С GOOGLE SHEETS " + "="*30)

        try:
            if not hasattr(self.app.state, 'background_tasks_runner'):
                 print("\nОшибка: AsyncBackgroundTasks runner не найден в app.state.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return
            if not hasattr(self.app.state, 'config_manager'):
                 print("\nОшибка: ConfigManager не найден в app.state.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return
            if not hasattr(self.app.state, 'settings'):
                 print("\nОшибка: Настройки (settings) не найдены в app.state.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return

            bg_runner: 'AsyncBackgroundTasks' = self.app.state.background_tasks_runner
            settings = self.app.state.settings
            # Импортируем config_manager для доступа к DEFAULT_SETTINGS
            from . import config_manager as cm_module

            sync_enabled = settings.get("enable_google_sheet_sync", cm_module.DEFAULT_SETTINGS['enable_google_sheet_sync'])
            sheet_url = settings.get("google_sheet_export_url", cm_module.DEFAULT_SETTINGS['google_sheet_export_url'])

            if not sync_enabled:
                 print("\nСинхронизация с Google Sheets отключена в config.ini.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return
            if not sheet_url:
                 print("\nURL Google Sheets (google_sheet_export_url) не указан в config.ini.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return

            print(f"\nURL: {sheet_url}")
            print("Запуск ручной синхронизации (может занять некоторое время)...")

            # Вызываем метод экземпляра через хелпер
            # trigger_google_sheet_sync_now теперь возвращает bool (были ли изменения)
            sync_result = self._run_async_and_get_result(
                bg_runner.trigger_google_sheet_sync_now()
            )

            if sync_result is True:
                print("\nСинхронизация завершена. Обнаружены и применены изменения.")
            elif sync_result is False:
                 print("\nСинхронизация завершена. Изменений не обнаружено или произошла ошибка (см. логи).")
            else: # sync_result is None (ошибка в _run_async_and_get_result)
                print("\nОшибка во время выполнения асинхронной операции синхронизации.")

        except Exception as e:
             print(f"\nНеожиданная ошибка при подготовке к синхронизации: {e!r}")

        try:
            input("\nНажмите Enter для возврата в главное меню...")
        except EOFError:
            print("\nОшибка ввода (EOF). Завершение работы меню...")
            self.stop()


    # Исправлено: Получение данных из KeyManager и AsyncBackgroundTasks в app.state.
    # Решение: Используются self.app.state.key_manager_instance и self.app.state.background_tasks_runner.
    def _show_status(self):
        """Показывает статус сервера."""
        self._clear_screen() # Очищаем перед показом
        print("\n" + "="*30 + " СТАТУС " + "="*30)

        available_keys = "Ошибка"
        total_keys = "Ошибка"
        bg_tasks_status = "Ошибка"
        server_status = "Ошибка"

        try:
            if hasattr(self.app.state, 'key_manager_instance'):
                km_instance: 'KeyManager' = self.app.state.key_manager_instance
                # Получаем данные асинхронно
                available_keys_res = self._run_async_and_get_result(km_instance.get_available_keys_count())
                total_keys_res = self._run_async_and_get_result(km_instance.get_total_keys_count())
                available_keys = available_keys_res if available_keys_res is not None else "Ошибка"
                total_keys = total_keys_res if total_keys_res is not None else "Ошибка"
            else:
                 available_keys = "N/A (KeyManager не готов)"
                 total_keys = "N/A (KeyManager не готов)"

            if hasattr(self.app.state, 'background_tasks_runner'):
                bg_runner: 'AsyncBackgroundTasks' = self.app.state.background_tasks_runner
                # is_running - синхронная проверка
                bg_tasks_status = "активны" if bg_runner.is_running() else "неактивны"
                server_status = "работает" if bg_runner.is_running() else "остановлен"
            else:
                 bg_tasks_status = "N/A (Runner не готов)"
                 server_status = "N/A (Runner не готов)"

        except Exception as e:
             print(f"\nОшибка при получении статуса: {e!r}")

        status_info = {
            "Статус сервера": server_status,
            "Доступных ключей": available_keys,
            "Всего ключей": total_keys,
            "Фоновые задачи": bg_tasks_status
        }

        for name, value in status_info.items():
            print(f"{name:20}: {value}")

        try:
            input("\nНажмите Enter для продолжения...")
        except EOFError:
            print("\nОшибка ввода (EOF). Завершение работы меню...")
            self.stop()

    # Исправлено: Получение статистики из KeyManager в app.state.
    # Решение: Используется self.app.state.key_manager_instance.
    def _show_stats(self):
        """Показывает статистику работы."""
        self._clear_screen()
        print("\n" + "="*30 + " СТАТИСТИКА КЛЮЧЕЙ " + "="*30)

        key_stats_list: Optional[List[Dict]] = None
        available_keys: Optional[int] = None
        total_requests: Optional[int] = None

        try:
            if hasattr(self.app.state, 'key_manager_instance'):
                km_instance: 'KeyManager' = self.app.state.key_manager_instance
                # Получаем данные асинхронно
                key_stats_list = self._run_async_and_get_result(km_instance.get_key_stats())
                available_keys = self._run_async_and_get_result(km_instance.get_available_keys_count())
                total_requests = self._run_async_and_get_result(km_instance.get_total_requests())
            else:
                 print("\nОшибка: KeyManager не найден в app.state.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return

        except Exception as e:
             print(f"\nОшибка при получении статистики KeyManager: {e!r}")
             try:
                 input("\nНажмите Enter для продолжения...")
             except EOFError:
                 self.stop(); return
             return


        if key_stats_list is None or available_keys is None or total_requests is None:
             print("\nНе удалось получить полную статистику (ошибка асинхронной операции).")
             try:
                 input("\nНажмите Enter для продолжения...")
             except EOFError:
                 self.stop(); return
             return

        total_keys = len(key_stats_list)
        bad_keys_count = total_keys - available_keys

        print(f"\nОбщая статистика:")
        print(f"  Всего ключей:        {total_keys}")
        print(f"  Доступных ключей:    {available_keys}")
        print(f"  Неисправных ключей:  {bad_keys_count}")
        print(f"  Всего запросов:      {total_requests}")

        # if key_stats: # Удалено, т.к. key_stats больше не используется напрямую
        if key_stats_list:
            print("\nСтатистика по ключам:")
            # Сортируем список словарей по ключу 'key'
            sorted_key_stats = sorted(key_stats_list, key=lambda x: x.get('key', ''))
            for data in sorted_key_stats:
                key = data.get("key", "N/A")
                status = data.get("status", "N/A")
                # Используем новые поля usage_count и error_message
                usage_count = data.get("usage_count", 0)
                error_message = data.get("error_message")
                last_used_ts = data.get("last_used")
                last_used_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_used_ts)) if last_used_ts else "N/A"

                status_str = f"[{status.upper()}]"
                details_str = ""
                if status == "good":
                    status_str = f"\033[92m{status_str}\033[0m" # Зеленый
                    details_str = f"Запросов: {usage_count:<5}"
                elif status == "bad":
                    status_str = f"\033[91m{status_str}\033[0m" # Красный
                    details_str = f"Ошибка: {str(error_message)[:50]}" if error_message else "Ошибка: N/A" # Обрезаем длинные ошибки
                else:
                     details_str = f"Статус: {status}" # Неизвестный статус

                print(f"  - {key[:8]}...{key[-4:]}: {status_str} | {details_str} | Посл. исп.: {last_used_str}")
        else:
            print("\nНет данных по ключам.")

        print("\n" + "="*70)
        try:
            input("Нажмите Enter для возврата в главное меню...")
        except EOFError:
            print("\nОшибка ввода (EOF). Завершение работы меню...")
            self.stop()

    # Исправлено: Получение логов из LoggerSetup в app.state.
    # Решение: Используется self.app.state.logger.get_last_logs().
    def _show_logs(self, lines_to_show=20):
        """Показывает последние N строк логов."""
        self._clear_screen()
        print("\n" + "="*30 + f" ПОСЛЕДНИЕ {lines_to_show} СТРОК ЛОГОВ " + "="*30)

        log_lines: Optional[List[str]] = None
        try:
            if hasattr(self.app.state, 'logger'):
                logger_instance: 'LoggerSetup' = self.app.state.logger
                # Вызываем асинхронную функцию
                log_lines = self._run_async_and_get_result(
                    logger_instance.get_last_logs(lines=lines_to_show)
                )
            else:
                 print("\nОшибка: LoggerSetup не найден в app.state.")

        except Exception as e:
             print(f"\nОшибка при получении логгера: {e!r}")


        if log_lines:
            # Добавляем перенос строки, если его нет
            print("\n".join(log_lines))
        else:
            # Сообщение об ошибке будет выведено из _run_async_and_get_result или get_last_logs
            print("\nЛог-файл пуст или не найден / Ошибка чтения.")

        print("\n" + "="*70)
        try:
            input("Нажмите Enter для возврата в главное меню...")
        except EOFError:
            print("\nОшибка ввода (EOF). Завершение работы меню...")
            self.stop()

    # Исправлено: Получение KeyManager и сессии из app.state. Обновлены вызовы методов.
    # Решение: Используются self.app.state.key_manager_instance и self.app.state.http_session.
    def _manage_keys(self):
        """Меню управления ключами."""
        km_instance: Optional['KeyManager'] = None
        session: Optional['aiohttp.ClientSession'] = None

        try:
            if hasattr(self.app.state, 'key_manager_instance'):
                km_instance = self.app.state.key_manager_instance
            else:
                 print("\nОшибка: KeyManager не найден в app.state.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return

            if hasattr(self.app.state, 'http_session'):
                 session = self.app.state.http_session
                 if not session or session.closed:
                      print("\nОшибка: HTTP сессия в app.state закрыта или недоступна.")
                      try:
                          input("\nНажмите Enter для продолжения...")
                      except EOFError:
                          self.stop(); return
                      return
            else:
                 print("\nОшибка: HTTP сессия не найдена в app.state.")
                 try:
                     input("\nНажмите Enter для продолжения...")
                 except EOFError:
                     self.stop(); return
                 return

        except Exception as e:
             print(f"\nОшибка при получении менеджеров/сессии: {e!r}")
             try:
                 input("\nНажмите Enter для продолжения...")
             except EOFError:
                 self.stop(); return
             return


        while self.running: # Цикл подменю (проверяем self.running)
            self._clear_screen()
            print("\n" + "="*30 + " УПРАВЛЕНИЕ КЛЮЧАМИ " + "="*30)

            # Получаем статистику асинхронно через экземпляр
            key_stats_list: Optional[List[Dict]] = self._run_async_and_get_result(km_instance.get_key_stats())

            if key_stats_list is None:
                 print("\nНе удалось получить список ключей.")
                 key_data_map = {} # Пустой словарь для дальнейшей логики
            else:
                 # Преобразуем список словарей в словарь для удобного доступа по ключу
                 key_data_map = {item.get('key'): item for item in key_stats_list if item.get('key')}

            sorted_keys = sorted(key_data_map.keys())

            if not sorted_keys:
                print("\nНет ключей для управления.")
            else:
                print("\nТекущие ключи:")
                for i, key in enumerate(sorted_keys):
                    data = key_data_map.get(key, {}) # Безопасный доступ
                    status = data.get("status", "N/A")
                    status_str = f"[{status.upper()}]"
                    if status == "good": status_str = f"\033[92m{status_str}\033[0m"
                    elif status == "bad": status_str = f"\033[91m{status_str}\033[0m"
                    print(f"  {i+1}. {key[:8]}...{key[-4:]} {status_str}")

            print("\nДействия:")
            print("  [a] Добавить новый ключ")
            if sorted_keys:
                print("  [d] Удалить ключ")
                print("  [v] Валидировать ключ")
            print("  [b] Назад в главное меню")

            choice = None
            try:
                choice = input("\nВыберите действие: ").lower()
            except EOFError:
                print("\nОшибка ввода (EOF). Завершение работы меню...")
                self.stop(); break # Выход из цикла подменю

            if choice == 'a':
                new_key = None
                try:
                    new_key = input("Введите новый ключ OpenRouter (sk-or-...): ").strip()
                except EOFError:
                    print("\nОшибка ввода (EOF). Завершение работы меню...")
                    self.stop(); break
                if new_key:
                    result = self._run_async_and_get_result(
                    km_instance.add_key(new_key) # keys_file больше не нужен
                )
                    if result is True:
                        print("\nКлюч успешно добавлен.")
                    elif result is False: # Явно проверяем False
                        print("\nНе удалось добавить ключ (возможно, он некорректен или уже существует и 'good').")
                    # Если result is None, сообщение об ошибке уже выведено
                try:
                    input("Нажмите Enter для продолжения...")
                except EOFError:
                    self.stop(); break
            elif choice == 'd' and sorted_keys:
                num_str = None
                try:
                    num_str = input(f"Введите номер ключа для удаления (1-{len(sorted_keys)}): ")
                    num = int(num_str)
                    if 1 <= num <= len(sorted_keys):
                        key_to_delete = sorted_keys[num-1]
                        confirm = None
                        try:
                            confirm = input(f"Вы уверены, что хотите удалить ключ {key_to_delete[:8]}...? (y/n): ").lower()
                        except EOFError:
                            print("\nОшибка ввода (EOF). Завершение работы меню...")
                            self.stop(); break
                        if confirm == 'y':
                            result = self._run_async_and_get_result(
                                km_instance.delete_key(key_to_delete)
                            )
                            if result is True:
                                print("\nКлюч успешно удален.")
                            elif result is False:
                                print("\nНе удалось удалить ключ (возможно, он уже был удален).")
                        else:
                            print("\nУдаление отменено.")
                    else:
                        print("\nНеверный номер ключа.")
                except ValueError:
                    print("\nНеверный ввод. Введите номер.")
                except EOFError:
                    print("\nОшибка ввода (EOF). Завершение работы меню...")
                    self.stop(); break
                try:
                    input("Нажмите Enter для продолжения...")
                except EOFError:
                    self.stop(); break
            elif choice == 'v' and sorted_keys:
                num_str = None
                try:
                    num_str = input(f"Введите номер ключа для валидации (1-{len(sorted_keys)}): ")
                    num = int(num_str)
                    if 1 <= num <= len(sorted_keys):
                        key_to_validate = sorted_keys[num-1]
                        print(f"\nВалидация ключа {key_to_validate[:8]}...")
                        result = self._run_async_and_get_result(
                            km_instance.manual_validate_key(key_to_validate, session)
                        )
                        if result is not None:
                             is_valid, msg = result
                             print(f"\nРезультат: {msg}")
                        # Если result is None, сообщение об ошибке уже выведено
                    else:
                        print("\nНеверный номер ключа.")
                except ValueError:
                    print("\nНеверный ввод. Введите номер.")
                except EOFError:
                    print("\nОшибка ввода (EOF). Завершение работы меню...")
                    self.stop(); break
                try:
                    input("Нажмите Enter для продолжения...")
                except EOFError:
                    self.stop(); break
            elif choice == 'b':
                break # Выход из цикла подменю
            elif choice is not None: # Обрабатываем только если был ввод
                print("\nНеверный выбор.")
                try:
                    input("Нажмите Enter для продолжения...")
                except EOFError:
                    self.stop(); break

    def _clear_screen(self):
        """Очищает экран консоли (простой вариант)."""
        # Для кроссплатформенности можно использовать os.system('cls' if os.name == 'nt' else 'clear')
        # но это может быть небезопасно. Простая печать новых строк - безопаснее.
        print("\n" * 80) # Печатаем много новых строк для имитации очистки

# --- Функция инициализации ---
# Теперь принимает loop и app
def init_menu(loop: asyncio.AbstractEventLoop, app: FastAPI) -> Menu:
    """Инициализирует и возвращает меню для приложения."""
    menu = Menu(loop, app)
    # Здесь можно добавить дополнительные настройки меню, если нужно
    return menu
