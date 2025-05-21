# -*- coding: utf-8 -*-
"""Модуль для настройки логирования."""

import logging
import os
import asyncio # Добавляем asyncio
from typing import Optional, Dict, Any, List

import aiofiles # Добавляем aiofiles

class LoggerSetup:
    def __init__(self):
        self.enabled = False
        self.log_file = ""
        self.logger = logging.getLogger('openrouter_proxy')
        self.logger.setLevel(logging.DEBUG) # Устанавливаем базовый уровень DEBUG, фильтрация будет на уровне обработчика
        self.is_setup = False # Флаг для проверки, был ли вызван setup_logging
        self.handlers: List[logging.Handler] = [] # Храним ссылки на обработчики

    def setup_logging(self, file_path: str, level: str = "INFO"):
        """Настраивает логирование в файл."""
        # Удаляем старые обработчики перед добавлением новых
        for handler in self.handlers:
             self.logger.removeHandler(handler)
             handler.close()
        self.handlers.clear()

        try:
            self.log_file = file_path
            # Используем базовый logging для начальных шагов настройки
            logging.info(f"[LoggerSetup] Попытка настройки логгера для файла: '{file_path}' с уровнем '{level}'")
            log_level = getattr(logging, level.upper(), logging.INFO)
            logging.info(f"[LoggerSetup] Установлен уровень логирования: {logging.getLevelName(log_level)}")
            # Улучшенный форматтер, включающий миллисекунды
            formatter = logging.Formatter(
                "%(asctime)s,%(msecs)03d | %(levelname)-8s | Key: %(key)-15s | Model: %(model)-30s | Status: %(status)-10s | Duration: %(duration)7.3f | %(message)s",
                datefmt='%Y-%m-%d %H:%M:%S' # Основной формат даты/времени
            )

            # Создаем директорию для логов, если она не существует
            log_dir = os.path.dirname(file_path)
            logging.info(f"[LoggerSetup] Определена директория для логов: '{log_dir}'")
            if log_dir: # Создаем только если путь не пустой (т.е. лог не в корне)
                if not os.path.exists(log_dir):
                    logging.info(f"[LoggerSetup] Директория '{log_dir}' не существует. Попытка создания...")
                    try:
                        os.makedirs(log_dir, exist_ok=True)
                        logging.info(f"[LoggerSetup] Директория для логов успешно создана: {log_dir}")
                    except OSError as e:
                        logging.error(f"[LoggerSetup] НЕ УДАЛОСЬ создать директорию для логов '{log_dir}': {e}")
                        # Можно либо выйти, либо продолжить и надеяться, что файл создастся в текущей директории
                else:
                    logging.info(f"[LoggerSetup] Директория '{log_dir}' уже существует.")

            # Добавляем filemode='w' для очистки лога при запуске
            logging.info(f"[LoggerSetup] Попытка создания FileHandler для '{file_path}' (mode='w', encoding='utf-8')")
            file_handler = logging.FileHandler(file_path, mode='w', encoding='utf-8') # Указываем кодировку и режим 'w'
            logging.info(f"[LoggerSetup] FileHandler создан. Установка форматтера и уровня...")
            file_handler.setFormatter(formatter)
            file_handler.setLevel(log_level) # Устанавливаем уровень для обработчика
            logging.info(f"[LoggerSetup] Форматтер и уровень установлены для FileHandler.")

            # Добавляем обработчик к логгеру и сохраняем ссылку
            logging.info(f"[LoggerSetup] Добавление FileHandler к логгеру '{self.logger.name}'...")
            self.logger.addHandler(file_handler)
            self.handlers.append(file_handler)
            logging.info(f"[LoggerSetup] FileHandler успешно добавлен.")

            # Опционально: добавить вывод в консоль для наглядности
            # console_handler = logging.StreamHandler()
            # console_handler.setFormatter(formatter)
            # console_handler.setLevel(log_level)
            # self.logger.addHandler(console_handler)
            # self.handlers.append(console_handler)

            self.logger.setLevel(logging.DEBUG) # Устанавливаем DEBUG на логгере, фильтрация на обработчиках
            self.is_setup = True # Используем is_setup вместо enabled

            self.log_info("Логирование успешно настроено",
                         key="system",
                         model="setup",
                         status=level.upper(),
                         duration=0)
        except Exception as e:
            # Используем базовый logging, если наш логгер не настроился
            logging.basicConfig(level=logging.ERROR) # Убедимся, что есть куда логировать
            logging.exception(f"Критическая ошибка настройки логирования для файла '{file_path}':")
            self.is_setup = False

    def log_info(self, message: str, **kwargs):
        """Логирует информационное сообщение."""
        if not self.is_setup: return # Проверяем флаг is_setup

        extra = {
            'key': kwargs.get('key', 'system'),
            'model': kwargs.get('model', 'system'),
            'status': kwargs.get('status', 'INFO'), # Используем стандартные уровни
            'duration': kwargs.get('duration', 0.0) # Используем float
        }
        # Передаем exc_info=True, если передан аргумент exc_info
        self.logger.info(message, extra=extra, exc_info=kwargs.get('exc_info', False))

    def log_warning(self, message: str, **kwargs):
        """Логирует предупреждение."""
        if not self.is_setup: return

        extra = {
            'key': kwargs.get('key', 'system'),
            'model': kwargs.get('model', 'system'),
            'status': kwargs.get('status', 'WARNING'),
            'duration': kwargs.get('duration', 0.0)
        }
        self.logger.warning(message, extra=extra, exc_info=kwargs.get('exc_info', False))

    def log_error(self, message: str, **kwargs):
        """Логирует ошибку."""
        if not self.is_setup: return

        extra = {
            'key': kwargs.get('key', 'system'),
            'model': kwargs.get('model', 'system'),
            'status': kwargs.get('status', 'ERROR'),
            'duration': kwargs.get('duration', 0.0)
        }
        # Передаем exc_info=True по умолчанию для log_error, если не указано иное
        self.logger.error(message, extra=extra, exc_info=kwargs.get('exc_info', True))

    def log_exception(self, message: str, **kwargs):
        """Логирует исключение с трейсбэком (синоним log_error с exc_info=True)."""
        if not self.is_setup: return

        extra = {
            'key': kwargs.get('key', 'system'),
            'model': kwargs.get('model', 'system'),
            'status': kwargs.get('status', 'EXCEPTION'), # Можно использовать свой статус
            'duration': kwargs.get('duration', 0.0)
        }
        # logger.exception автоматически добавляет exc_info=True
        self.logger.exception(message, extra=extra)

    def log_debug(self, message: str, **kwargs):
        """Логирует отладочное сообщение."""
        if not self.is_setup: return

        extra = {
            'key': kwargs.get('key', 'system'),
            'model': kwargs.get('model', 'system'),
            'status': kwargs.get('status', 'DEBUG'),
            'duration': kwargs.get('duration', 0.0)
        }
        self.logger.debug(message, extra=extra, exc_info=kwargs.get('exc_info', False))

    # Исправлено: Оптимизирован метод get_last_logs для чтения с конца файла.
    # Решение: Используется чтение блоками с конца файла с помощью aiofiles.
    async def get_last_logs(self, lines: int = 20) -> List[str]:
        """
        Асинхронно и эффективно возвращает последние N строк логов, читая файл с конца.
        """
        if not self.log_file:
            return ["Лог-файл не настроен"]
        if lines <= 0:
            return []

        try:
            # Проверяем существование файла асинхронно
            if not await aiofiles.os.path.exists(self.log_file):
                return [f"Лог-файл не найден: {self.log_file}"]

            # Исправлено: Получаем размер файла через aiofiles.os.stat перед открытием.
            # Решение: Используем await aiofiles.os.stat(self.log_file).
            file_stat = await aiofiles.os.stat(self.log_file)
            file_size = file_stat.st_size
            if file_size == 0:
                return ["Лог-файл пуст"]

            async with aiofiles.open(self.log_file, "rb") as f: # Открываем в бинарном режиме для seek

                chunk_size = 1024 * 4 # Размер блока для чтения (4KB)
                buffer = b""
                found_lines: List[bytes] = []
                lines_needed = lines + 1 # Ищем на одну строку больше, чтобы обработать последнюю неполную строку
                seek_pos = file_size

                while len(found_lines) < lines_needed and seek_pos > 0:
                    # Определяем позицию для чтения следующего блока
                    read_start = max(0, seek_pos - chunk_size)
                    await f.seek(read_start)
                    chunk = await f.read(seek_pos - read_start)

                    # Добавляем новый блок к началу буфера
                    buffer = chunk + buffer

                    # Ищем разделители строк в буфере
                    # Ищем справа налево, чтобы получить последние строки первыми
                    new_lines = buffer.splitlines(keepends=True)

                    # Обновляем список найденных строк
                    # Берем строки с конца new_lines, пока не наберем нужное количество
                    potential_lines = new_lines[::-1] # Переворачиваем для удобства
                    found_lines = []
                    current_buffer_part = b""
                    for line in potential_lines:
                        if len(found_lines) < lines_needed:
                             found_lines.append(line)
                        else:
                             current_buffer_part = line + current_buffer_part # Сохраняем остаток для следующей итерации

                    buffer = current_buffer_part # Оставляем в буфере только то, что не вошло в строки

                    # Перемещаем позицию для следующего чтения
                    seek_pos = read_start

                    # Если прочитали весь файл и все еще не хватает строк
                    if seek_pos == 0 and len(found_lines) < lines_needed:
                         # Добавляем остаток буфера как первую строку, если он есть
                         if buffer:
                              found_lines.append(buffer)
                         break # Выходим из цикла

                # Декодируем найденные строки и возвращаем нужное количество
                decoded_lines = [line.decode('utf-8', errors='replace').strip() for line in found_lines[::-1]] # Возвращаем в правильном порядке

                # Убираем пустые строки, которые могли появиться из-за splitlines
                decoded_lines = [line for line in decoded_lines if line]

                return decoded_lines[-lines:] # Возвращаем последние N строк

        except Exception as e:
            self.log_error(f"Ошибка асинхронного чтения логов из '{self.log_file}': {repr(e)}", exc_info=False)
            return [f"Ошибка чтения логов: {repr(e)}"]

# Глобальный экземпляр логгера
logger = LoggerSetup()
