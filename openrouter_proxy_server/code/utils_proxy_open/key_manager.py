# utils_proxy_open/key_manager.py
# -*- coding: utf-8 -*-
"""Асинхронный модуль для управления ключами API (keys.csv) с использованием класса."""

import asyncio
import random
import os
import time
import json
import csv # Добавлено для парсинга CSV из Google Sheets
import io # Добавлено для работы с текстовым потоком CSV
from typing import Dict, List, Tuple, Optional, Set, Union, Any # Добавлен импорт Any
from dataclasses import dataclass, field

import aiofiles
import aiohttp
import aiofiles.os

from . import logger_setup
# Исправлено: Удален некорректный импорт ConfigManager.
# Решение: Убран импорт класса, т.к. config_manager - это модуль.
# from .config_manager import ConfigManager

# Исправлено: Заменено поле counter_or_error на usage_count и error_message для ясности.
# Решение: Разделены счетчик и сообщение об ошибке на два отдельных поля.
# Добавлено: Поле marked_bad_timestamp для хранения времени пометки ключа как 'bad'.
@dataclass
class ApiKey:
    key: str
    status: str = "good"
    usage_count: int = 0 # Счетчик успешных использований для 'good' ключей
    error_message: Optional[str] = None # Сообщение об ошибке для 'bad' ключей
    last_used: float = 0.0
    recovery_timestamp: Optional[float] = None # Время (timestamp), после которого можно пытаться восстановить ключ (для ошибок 429)
    marked_bad_timestamp: Optional[float] = None # Время (timestamp), когда ключ был помечен как 'bad'

class KeyManager:
    def __init__(self, keys_file_path: str, api_url: str, validation_timeout: int = 10):
        self.keys_file_path = keys_file_path
        self.api_url = api_url
        self.validation_timeout = validation_timeout
        self.keys: List[ApiKey] = []
        self.lock = asyncio.Lock()
        self._keys_dirty = False
        logger_setup.logger.log_info(f"KeyManager инициализирован для файла: {keys_file_path}")

    # --- Методы для получения статистики ---

    async def get_total_keys_count(self) -> int:
        """Возвращает общее количество ключей."""
        async with self.lock:
            return len(self.keys)

    async def get_available_keys_count(self) -> int:
        """Возвращает количество доступных ('good') ключей."""
        async with self.lock:
            return len([k for k in self.keys if k.status == "good"])

    # Исправлено: Обновлен метод для использования usage_count.
    # Решение: Суммируются значения поля usage_count.
    async def get_total_requests(self) -> int:
        """Возвращает общее количество успешных запросов по всем 'good' ключам."""
        async with self.lock:
            return sum(k.usage_count for k in self.keys if k.status == "good")

    async def get_bad_keys_list(self) -> List[str]:
        """Возвращает список ключей со статусом 'bad'."""
        async with self.lock:
            return [k.key for k in self.keys if k.status == "bad"]

    # Исправлено: Обновлен метод для возврата usage_count и error_message.
    # Решение: Включены новые поля в возвращаемый словарь.
    async def get_key_stats(self) -> List[Dict]:
        """Возвращает список словарей со статистикой по всем ключам."""
        async with self.lock:
            return [
                {
                    "key": k.key,
                    "status": k.status,
                    "usage_count": k.usage_count,
                    "error_message": k.error_message,
                    "last_used": k.last_used,
                    "recovery_timestamp": k.recovery_timestamp,
                    "marked_bad_timestamp": k.marked_bad_timestamp # Добавляем новое поле
                }
                for k in self.keys
            ]

    # --- Методы для работы с файлом keys.csv ---

    async def _create_default_keys_file(self):
        """Приватный метод: создает файл keys.csv с заголовком и примером, если он отсутствует."""
        if await aiofiles.os.path.exists(self.keys_file_path):
            return
        # Обновлено: Заголовок включает новое поле marked_bad_at
        try:
            async with aiofiles.open(self.keys_file_path, "w", newline="", encoding='utf-8') as f:
                await f.write("key;status;usage_count_or_error_msg;marked_bad_at\n") # Новый заголовок
                await f.write("sk-or-v1-replace-this-example-key;good;0;\n") # Пример с пустым последним полем
            logger_setup.logger.log_info(f"Создан файл ключей по умолчанию: '{self.keys_file_path}'")
        except IOError as e:
            logger_setup.logger.log_error(f"Не удалось создать файл ключей '{self.keys_file_path}': {e}")
        except Exception as e:
            logger_setup.logger.log_exception(f"Непредвиденная ошибка при создании файла ключей: {e}")

    async def save_keys(self, file_path: Optional[str] = None):
        """Асинхронно сохраняет текущее состояние ключей в keys.csv."""
        target_path = file_path or self.keys_file_path
        keys_to_save: List[ApiKey] = []
        needs_saving = False

        async with self.lock:
            if self._keys_dirty:
                needs_saving = True
                keys_to_save = self.keys[:] # Копируем список ключей
                self._keys_dirty = False # Оптимистично сбрасываем флаг
            else:
                return # Выходим, если сохранять нечего

        save_successful = False
        # Обновлено: Сохраняем key;status;value;marked_bad_at_str
        temp_file_path = target_path + ".tmp"
        try:
            async with aiofiles.open(temp_file_path, "w", newline="", encoding='utf-8') as f:
                await f.write("key;status;usage_count_or_error_msg;marked_bad_at\n") # Новый заголовок
                # Сортируем ключи для консистентности файла
                sorted_keys_data = sorted(keys_to_save, key=lambda x: x.key)
                for k_data in sorted_keys_data:
                    key_safe = str(k_data.key).replace(';', ',')
                    status_safe = str(k_data.status).replace(';', ',')
                    value_to_save = ""
                    marked_bad_at_str = "" # Строка для времени пометки 'bad'

                    if k_data.status == "good":
                        value_to_save = str(k_data.usage_count)
                    elif k_data.status == "bad":
                        value_to_save = str(k_data.error_message or "")
                        # Форматируем timestamp пометки 'bad', если он есть
                        if k_data.marked_bad_timestamp:
                            try:
                                # Формат YYYY-MM-DD HH:MM:SS
                                marked_bad_at_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(k_data.marked_bad_timestamp))
                            except Exception: # На случай некорректного timestamp
                                marked_bad_at_str = "invalid_timestamp"

                    value_safe = value_to_save.replace(';', ',').replace('\n', ' ') # Доп. очистка
                    marked_bad_at_safe = marked_bad_at_str.replace(';', ',')

                    await f.write(f"{key_safe};{status_safe};{value_safe};{marked_bad_at_safe}\n") # Добавляем новое поле

            await aiofiles.os.replace(temp_file_path, target_path)
            save_successful = True
            if needs_saving: # Логируем только если были изменения для сохранения
                 logger_setup.logger.log_info(f"Ключи успешно сохранены в '{target_path}'.")

        except (IOError, OSError) as e:
            logger_setup.logger.log_error(f"Ошибка при сохранении ключей в '{target_path}': {e}")
            if await aiofiles.os.path.exists(temp_file_path):
                try:
                    await aiofiles.os.remove(temp_file_path)
                except OSError as remove_err:
                    logger_setup.logger.log_error(f"Не удалось удалить временный файл '{temp_file_path}': {remove_err}")
        except Exception as e:
             logger_setup.logger.log_exception(f"Непредвиденная ошибка при сохранении ключей: {e}")
        finally:
            if not save_successful and needs_saving:
                async with self.lock:
                    self._keys_dirty = True # Восстанавливаем флаг
                logger_setup.logger.log_warning("Восстановлен флаг _keys_dirty из-за ошибки сохранения.")
        return save_successful

    async def load_keys(self, validate_on_load: bool = False, session: Optional[aiohttp.ClientSession] = None):
        """
        Асинхронно загружает ключи из keys.csv и опционально валидирует их.
        Обновляет состояние экземпляра KeyManager.
        """
        logger_setup.logger.log_info(f"Асинхронная загрузка ключей из '{self.keys_file_path}'...")
        await self._create_default_keys_file()

        new_keys_list: List[ApiKey] = []
        keys_to_validate: List[ApiKey] = []
        initial_dirty_state = False

        try:
            async with aiofiles.open(self.keys_file_path, "r", newline="", encoding='utf-8') as f:
                # Обновлено: Парсим формат key;status;value;marked_bad_at
                line_num = 0
                header = "key;status;usage_count_or_error_msg;marked_bad_at" # Ожидаемый заголовок
                async for line in f:
                    line_num += 1
                    line = line.strip()
                    # Пропускаем пустые, комментарии и заголовок
                    if not line or line.startswith("#") or line.lower().strip() == header:
                        continue

                    parts = line.split(";", 3) # Разделяем на 4 части максимум
                    if len(parts) >= 1 and parts[0]:
                        key = parts[0].strip()
                        # Значения по умолчанию
                        status = "good"
                        value_str = ""
                        marked_bad_at_str = ""

                        if len(parts) > 1: status = parts[1].strip().lower()
                        if len(parts) > 2: value_str = parts[2].strip()
                        if len(parts) > 3: marked_bad_at_str = parts[3].strip() # Читаем 4-е поле

                        usage_count = 0
                        error_message = None
                        marked_bad_timestamp = None # Timestamp пометки 'bad'

                        if status == "good":
                            if value_str.isdigit():
                                usage_count = int(value_str)
                            else:
                                usage_count = 0
                                if value_str:
                                     logger_setup.logger.log_warning(f"Строка {line_num}: Некорректный счетчик '{value_str}' для 'good' ключа '{key[:10]}...'. Установлен в 0.")
                        elif status == "bad":
                            error_message = value_str if value_str else "Marked as bad"
                            # Пытаемся распарсить timestamp пометки 'bad' из строки
                            if marked_bad_at_str:
                                try:
                                    # Ожидаемый формат: YYYY-MM-DD HH:MM:SS
                                    marked_bad_timestamp = time.mktime(time.strptime(marked_bad_at_str, '%Y-%m-%d %H:%M:%S'))
                                except (ValueError, TypeError):
                                    logger_setup.logger.log_warning(f"Строка {line_num}: Некорректный формат времени '{marked_bad_at_str}' для 'bad' ключа '{key[:10]}...'.")
                        else:
                            logger_setup.logger.log_warning(f"Строка {line_num}: Неизвестный статус '{status}' для ключа '{key[:10]}...'. Установлен статус 'bad'.")
                            status = "bad"
                            error_message = f"Unknown status: {status}"
                            # Также пытаемся распарсить timestamp для неизвестного статуса, если он есть
                            if marked_bad_at_str:
                                try:
                                    marked_bad_timestamp = time.mktime(time.strptime(marked_bad_at_str, '%Y-%m-%d %H:%M:%S'))
                                except (ValueError, TypeError): pass # Игнорируем ошибку парсинга здесь

                        if not key.startswith("sk-or-"):
                            logger_setup.logger.log_warning(f"Строка {line_num}: Ключ '{key[:10]}...' не похож на ключ OpenRouter. Пропускается.")
                            continue

                        api_key_obj = ApiKey(
                            key=key,
                            status=status,
                            usage_count=usage_count,
                            error_message=error_message,
                            marked_bad_timestamp=marked_bad_timestamp # Сохраняем распарсенный timestamp
                            # recovery_timestamp не загружается из файла
                        )
                        new_keys_list.append(api_key_obj)

                        if validate_on_load and session:
                            keys_to_validate.append(api_key_obj) # Валидируем все при загрузке

        except IOError as e:
            logger_setup.logger.log_error(f"Ошибка чтения файла ключей '{self.keys_file_path}': {e}. Ключи не загружены.")
            return # Оставляем старые данные
        except Exception as e:
            logger_setup.logger.log_exception(f"Непредвиденная ошибка при загрузке ключей: {e}")
            return

        # --- Обновление состояния экземпляра ---
        async with self.lock:
            initial_dirty_state = self._keys_dirty
            self.keys = new_keys_list
            if not initial_dirty_state:
                 self._keys_dirty = False

        logger_setup.logger.log_info(f"Загружено {len(self.keys)} ключей. Доступно ('good'): {await self.get_available_keys_count()}.")

        # --- Валидация (если включена) ---
        if keys_to_validate:
            logger_setup.logger.log_info(f"Запуск асинхронной валидации для {len(keys_to_validate)} ключей...")
            validated_count = 0
            for key_obj in keys_to_validate:
                is_valid, error_msg = await self._validate_key_internal(key_obj.key, session) # Используем внутренний метод
                # Обновляем статус объекта ключа напрямую (лок не нужен, т.к. объект еще не используется)
                # Но update_key_status нужен для обновления списка активных ключей и флага dirty
                await self.update_key_status(key_obj.key, is_valid, error_msg, save_now=False) # Не сохраняем сразу
                validated_count += 1
                await asyncio.sleep(0.1) # Пауза

            logger_setup.logger.log_info(f"Валидация завершена. Проверено: {validated_count} ключей.")
            # Сохраняем изменения статусов после валидации, если они были
            needs_final_save = False
            async with self.lock:
                needs_final_save = self._keys_dirty
            if needs_final_save:
                await self.save_keys()

    # --- Методы для валидации и управления статусом ключей ---

    async def _validate_key_internal(self, key: str, session: aiohttp.ClientSession) -> Tuple[bool, str]:
        """Приватный метод для проверки валидности ключа через API."""
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        test_data = {"model": "openai/gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}], "max_tokens": 1}
        error_message = ""
        is_valid = False
        try:
            timeout = aiohttp.ClientTimeout(total=self.validation_timeout)
            async with session.post(self.api_url, headers=headers, json=test_data, timeout=timeout) as response:
                if response.status == 200:
                    is_valid = True
                else:
                    try:
                        error_body = await response.read()
                        try:
                            error_data = json.loads(error_body)
                            error_message = error_data.get("error", {}).get("message", error_body.decode('utf-8', errors='ignore'))
                        except json.JSONDecodeError:
                            error_message = error_body.decode('utf-8', errors='ignore')
                    except Exception as read_err:
                        error_message = f"Не удалось прочитать тело ошибки: {repr(read_err)}"
                    logger_setup.logger.log_warning(f"Ключ {key[:8]}... невалиден (status {response.status}): {error_message[:100]}")

        except asyncio.TimeoutError:
            error_message = f"Таймаут ({self.validation_timeout}s) при валидации ключа."
            logger_setup.logger.log_warning(f"Ключ {key[:8]}... ошибка валидации: {error_message}")
        except aiohttp.ClientError as e:
            error_message = f"Ошибка сети/клиента при валидации: {repr(e)}"
            logger_setup.logger.log_warning(f"Ключ {key[:8]}... ошибка валидации: {error_message}")
        except Exception as e:
             error_message = f"Непредвиденная ошибка при валидации: {repr(e)}"
             logger_setup.logger.log_exception(f"Ключ {key[:8]}... ошибка валидации: {error_message}")

        return is_valid, error_message

    async def update_key_status(
        self,
        key: str,
        is_good: bool,
        error_message: Optional[str] = None,
        save_now: bool = False,
        increment_count: bool = False,
        recovery_delay_sec: Optional[int] = None # Новый параметр для задержки восстановления
    ):
        """
        Обновляет статус ключа, флаг _keys_dirty и опционально устанавливает время восстановления.
        """
        made_dirty = False
        current_time = time.time()
        async with self.lock:
            # Обновлено: Устанавливается/сбрасывается marked_bad_timestamp.
            key_obj = next((k for k in self.keys if k.key == key), None)
            if not key_obj:
                logger_setup.logger.log_warning(f"Попытка обновить статус несуществующего ключа: {key[:8]}...")
                return

            current_status = key_obj.status
            new_status = "good" if is_good else "bad"
            status_changed = current_status != new_status

            if status_changed:
                log_msg_base = f"Статус ключа {key[:8]}... изменен: {current_status} -> {new_status}"
                log_details = ""
                if not is_good and error_message:
                    log_details += f" (Причина: {error_message[:50]})"

                key_obj.status = new_status
                made_dirty = True
                if is_good:
                    # При переходе в 'good' сбрасываем все "плохие" атрибуты
                    key_obj.error_message = None
                    key_obj.recovery_timestamp = None
                    key_obj.marked_bad_timestamp = None # Сбрасываем время пометки 'bad'
                    # Можно сбросить счетчик при восстановлении, или оставить. Оставим пока.
                    # key_obj.usage_count = 0
                else:
                    # При переходе в 'bad' записываем атрибуты
                    key_obj.error_message = error_message or "Marked as bad"
                    key_obj.usage_count = 0 # Сбрасываем счетчик
                    key_obj.marked_bad_timestamp = current_time # Устанавливаем время пометки 'bad'
                    # Устанавливаем время восстановления, если указана задержка
                    if recovery_delay_sec is not None and recovery_delay_sec > 0:
                        key_obj.recovery_timestamp = current_time + recovery_delay_sec
                        log_details += f" (восстановление через {recovery_delay_sec} сек)"
                    else:
                        # Если задержка не указана, сбрасываем timestamp восстановления
                        key_obj.recovery_timestamp = None

                logger_setup.logger.log_info(log_msg_base + log_details)

            # Инкрементируем счетчик только если ключ 'good' и статус не менялся на 'bad' в этом вызове
            # (чтобы не инкрементить при восстановлении, если не предусмотрено)
            if is_good and increment_count and (not status_changed or current_status == "good"):
                key_obj.usage_count += 1
                made_dirty = True

            key_obj.last_used = current_time

            if made_dirty:
                self._keys_dirty = True

        if save_now and made_dirty:
             await self.save_keys()

    async def recover_bad_keys(self, session: aiohttp.ClientSession) -> int:
        """
        Проверяет и пытается восстановить ключи со статусом 'bad',
        учитывая время восстановления (recovery_timestamp).
        """
        recovered_count = 0
        keys_to_check: List[ApiKey] = []
        current_time = time.time()
        async with self.lock:
            # Выбираем 'bad' ключи, у которых либо нет времени восстановления, либо оно уже прошло
            keys_to_check = [
                k for k in self.keys
                if k.status == "bad" and (k.recovery_timestamp is None or k.recovery_timestamp <= current_time)
            ]

        if not keys_to_check:
            logger_setup.logger.log_debug("Нет 'bad' ключей для восстановления.")
            return 0

        logger_setup.logger.log_info(f"Запуск асинхронного восстановления для {len(keys_to_check)} 'bad' ключей...")
        made_dirty_in_recovery = False

        for key_obj in keys_to_check:
            logger_setup.logger.log_debug(f"Проверка ключа {key_obj.key[:8]}...")
            is_valid, error_msg = await self._validate_key_internal(key_obj.key, session)
            if is_valid:
                # Обновляем статус, не сохраняя сразу
                await self.update_key_status(key_obj.key, is_good=True, error_message="", save_now=False)
                recovered_count += 1
                made_dirty_in_recovery = True
                logger_setup.logger.log_info(f"Ключ {key_obj.key[:8]}... восстановлен.")
            else:
                # Исправлено: Обновлена логика обновления ошибки для 'bad' ключей.
                # Решение: Используется поле error_message.
                # Обновляем сообщение об ошибке, если оно изменилось
                async with self.lock:
                    if key_obj.error_message != error_msg:
                        key_obj.error_message = error_msg
                        self._keys_dirty = True
                        made_dirty_in_recovery = True
                logger_setup.logger.log_debug(f"Ключ {key_obj.key[:8]}... все еще невалиден: {error_msg[:50]}")
            await asyncio.sleep(0.2) # Пауза

        if recovered_count > 0:
            logger_setup.logger.log_info(f"Восстановлено ключей: {recovered_count}.")
        else:
            logger_setup.logger.log_info("Не удалось восстановить ни одного ключа.")

        # Сохраняем все изменения после цикла, если они были
        if made_dirty_in_recovery:
            logger_setup.logger.log_debug("Сохранение ключей после цикла восстановления...")
            await self.save_keys()

        return recovered_count

    # --- Методы для управления ключами (добавление/удаление) ---

    async def add_key(self, key: str) -> bool:
        """Добавляет новый ключ и сохраняет в файл."""
        if not key or not isinstance(key, str) or not key.startswith("sk-or-"):
            logger_setup.logger.log_error(f"Попытка добавить невалидный ключ: '{key}'")
            return False

        key = key.strip()
        made_change = False
        async with self.lock:
            existing_key = next((k for k in self.keys if k.key == key), None)
            if existing_key:
                # Исправлено: Обновлена логика добавления/восстановления ключа.
                # Решение: Используются новые поля usage_count и error_message.
                logger_setup.logger.log_warning(f"Ключ {key[:8]}... уже существует.")
                if existing_key.status == "bad":
                     logger_setup.logger.log_info(f"Ключ {key[:8]}... был 'bad'. Меняем статус на 'good'.")
                     existing_key.status = "good"
                     existing_key.usage_count = 0 # Сбрасываем счетчик
                     existing_key.error_message = None # Сбрасываем ошибку
                     self._keys_dirty = True
                     made_change = True
                # else: # Уже 'good', ничего не делаем
            else:
                # Добавляем новый ключ с начальными значениями
                self.keys.append(ApiKey(key=key, status="good", usage_count=0, error_message=None))
                self._keys_dirty = True
                made_change = True
                logger_setup.logger.log_info(f"Ключ {key[:8]}... добавлен.")

        if made_change:
            await self.save_keys()
            return True
        return False

    async def delete_key(self, key: str) -> bool:
        """Удаляет ключ и сохраняет изменения."""
        key_found = False
        async with self.lock:
            initial_len = len(self.keys)
            self.keys = [k for k in self.keys if k.key != key]
            if len(self.keys) < initial_len:
                key_found = True
                self._keys_dirty = True
                logger_setup.logger.log_info(f"Ключ {key[:8]}... удален.")
            else:
                logger_setup.logger.log_warning(f"Попытка удалить несуществующий ключ: {key[:8]}...")

        if key_found:
            await self.save_keys()
            return True
        return False

    async def manual_validate_key(self, key: str, session: aiohttp.ClientSession) -> Tuple[bool, str]:
         """Выполняет ручную валидацию ключа и обновляет его статус с сохранением."""
         logger_setup.logger.log_info(f"Ручная асинхронная валидация ключа {key[:8]}...")
         is_valid, msg = await self._validate_key_internal(key, session)
         await self.update_key_status(key, is_good=is_valid, error_message=msg, save_now=True) # Сохраняем сразу

         result_msg = f"Ключ {key[:8]}... {'валиден' if is_valid else 'НЕ валиден'}."
         if not is_valid and msg:
              result_msg += f" Ошибка: {msg}"
         logger_setup.logger.log_info(result_msg)
         return is_valid, result_msg

    # --- Метод выбора ключа ---

    async def get_random_key(self) -> Optional[str]:
        """
        Возвращает случайный рабочий ключ ('good'), у которого не установлено
        активное время восстановления (recovery_timestamp).
        """
        current_time = time.time()
        async with self.lock:
            # Фильтруем 'good' ключи, исключая те, что временно заблокированы по recovery_timestamp
            available_keys = [
                k.key for k in self.keys
                if k.status == "good" and (k.recovery_timestamp is None or k.recovery_timestamp <= current_time)
            ]
            if not available_keys:
                # Если нет доступных с учетом recovery_timestamp, логируем это
                all_good_keys_count = len([k for k in self.keys if k.status == "good"])
                if all_good_keys_count > 0:
                     logger_setup.logger.log_warning(f"Нет доступных 'good' ключей. {all_good_keys_count} ключей временно заблокированы из-за лимитов (recovery_timestamp).")
                else:
                     logger_setup.logger.log_warning("Нет доступных 'good' ключей.")
                return None
            # Выбираем случайный из доступных
            try:
                key = random.choice(available_keys)
                return key
            except IndexError: # На случай, если список пуст (хотя проверка выше должна это предотвратить)
                 return None

    # --- Метод синхронизации с Google Sheets ---

    # Исправлено: Метод теперь принимает словарь настроек вместо объекта ConfigManager.
    # Решение: Тип аргумента 'config' изменен на Dict. Доступ к настройкам через get().
    async def sync_keys_from_google_sheet(self, session: aiohttp.ClientSession, settings: Dict[str, Any]) -> bool:
        """
        Асинхронно загружает ключи из Google Sheet (через экспорт CSV) и объединяет их с локальными.
        Добавляет новые ключи, найденные в таблице. Не удаляет локальные ключи, отсутствующие в таблице.

        Args:
            session: Экземпляр aiohttp.ClientSession.
            settings: Словарь с текущими настройками приложения.

        Returns:
            bool: True, если были внесены изменения в список ключей, иначе False.
        """
        # Получаем настройки из словаря settings
        enabled = settings.get("enable_google_sheet_sync", False)
        url = settings.get("google_sheet_export_url", "")
        key_column_index = settings.get("google_sheet_key_column_index", 0) # По умолчанию 0

        if not enabled or not url:
            logger_setup.logger.log_debug("Синхронизация с Google Sheets отключена или URL не указан.")
            return False

        logger_setup.logger.log_info(f"Запуск синхронизации ключей из Google Sheet: {url}")

        sheet_keys: Set[str] = set()
        try:
            timeout = aiohttp.ClientTimeout(total=30) # Таймаут для загрузки таблицы
            async with session.get(url, timeout=timeout) as response:
                response.raise_for_status() # Проверка на HTTP ошибки
                content = await response.text(encoding='utf-8') # Читаем как текст

                # Используем csv.reader для парсинга
                csv_data = io.StringIO(content)
                reader = csv.reader(csv_data)

                line_num = 0
                for row in reader:
                    line_num += 1
                    if not row: continue # Пропускаем пустые строки
                    try:
                        # Получаем ключ из нужной колонки (индекс с 0)
                        if key_column_index < len(row):
                            key = row[key_column_index].strip()
                            if key.startswith("sk-or-"):
                                sheet_keys.add(key)
                            elif key and line_num > 1: # Логируем предупреждение, если не похоже на ключ (и не заголовок)
                                logger_setup.logger.log_warning(f"GSheet строка {line_num}: Значение '{key[:20]}...' в колонке {key_column_index+1} не похоже на ключ OpenRouter.")
                        elif line_num > 1: # Логируем, если колонка отсутствует (и не заголовок)
                             logger_setup.logger.log_warning(f"GSheet строка {line_num}: Колонка {key_column_index+1} отсутствует.")
                    except IndexError:
                         logger_setup.logger.log_warning(f"GSheet строка {line_num}: Ошибка индекса при доступе к колонке {key_column_index+1}.")
                    except Exception as parse_err:
                         logger_setup.logger.log_warning(f"GSheet строка {line_num}: Ошибка парсинга строки CSV: {parse_err}")


        except aiohttp.ClientError as e:
            logger_setup.logger.log_error(f"Ошибка сети при загрузке Google Sheet CSV: {e}")
            return False
        except asyncio.TimeoutError:
            logger_setup.logger.log_error("Таймаут при загрузке Google Sheet CSV.")
            return False
        except Exception as e:
            logger_setup.logger.log_exception(f"Непредвиденная ошибка при синхронизации с Google Sheet: {e}")
            return False

        if not sheet_keys:
            logger_setup.logger.log_warning("Не найдено валидных ключей OpenRouter в Google Sheet.")
            return False

        logger_setup.logger.log_info(f"Найдено {len(sheet_keys)} ключей в Google Sheet.")

        # --- Объединение ключей ---
        added_count = 0
        restored_count = 0
        made_dirty_in_sync = False

        async with self.lock:
            current_keys_dict: Dict[str, ApiKey] = {k.key: k for k in self.keys}

            for sheet_key in sheet_keys:
                if sheet_key not in current_keys_dict:
                    # Добавляем новый ключ
                    new_key_obj = ApiKey(key=sheet_key, status="good", usage_count=0, error_message=None)
                    self.keys.append(new_key_obj)
                    logger_setup.logger.log_info(f"GSheet Sync: Добавлен новый ключ {sheet_key[:8]}...")
                    added_count += 1
                    made_dirty_in_sync = True
                else:
                    # Ключ уже существует, проверяем статус
                    existing_key = current_keys_dict[sheet_key]
                    if existing_key.status == "bad":
                        # Восстанавливаем ключ, если он есть в таблице
                        existing_key.status = "good"
                        existing_key.usage_count = 0 # Сбрасываем счетчик
                        existing_key.error_message = None # Сбрасываем ошибку
                        logger_setup.logger.log_info(f"GSheet Sync: Восстановлен 'bad' ключ {sheet_key[:8]}... так как он присутствует в таблице.")
                        restored_count += 1
                        made_dirty_in_sync = True

            if made_dirty_in_sync:
                self._keys_dirty = True

        logger_setup.logger.log_info(f"Синхронизация с Google Sheet завершена. Добавлено: {added_count}, Восстановлено 'bad': {restored_count}.")

        # Сохраняем изменения, если они были
        if made_dirty_in_sync:
            await self.save_keys()
            return True

        return False # Возвращаем False, если изменений не было
