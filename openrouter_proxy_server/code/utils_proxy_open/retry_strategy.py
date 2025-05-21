# -*- coding: utf-8 -*-
"""Модуль для реализации стратегий повторных попыток запросов."""

import asyncio
import random
from typing import TypeVar, Callable, Optional, Any, Dict, Set, Type
from dataclasses import dataclass
from datetime import datetime

from . import logger_setup
from .error_handler import ProxyError, RequestError, QuotaExceededError

# Тип для обобщенного результата функции
T = TypeVar('T')

@dataclass
class RetryStrategyConfig:
    """Конфигурация стратегии повторных попыток."""
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter_factor: float = 0.15
    retry_codes: Set[int] = frozenset({429, 500, 502, 503, 504})
    retry_exceptions: Set[Type[Exception]] = frozenset({RequestError, asyncio.TimeoutError})

class RetryStrategy:
    """Реализация стратегии повторных попыток с экспоненциальной задержкой."""

    def __init__(self, config: RetryStrategyConfig):
        self.config = config
        self._logger = logger_setup.logger

    def _calculate_delay(self, attempt: int) -> float:
        """Вычисляет время задержки для текущей попытки.

        Args:
            attempt: Номер попытки (начиная с 1).

        Returns:
            float: Время задержки в секундах.
        """
        # Базовая экспоненциальная задержка
        delay = self.config.base_delay * (self.config.exponential_base ** (attempt - 1))
        delay = min(delay, self.config.max_delay)

        # Добавляем случайное отклонение
        if self.config.jitter_factor > 0:
            jitter_range = delay * self.config.jitter_factor
            delay += random.uniform(-jitter_range, jitter_range)

        return max(0.1, delay)  # Минимальная задержка 100мс

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """Определяет, нужно ли выполнить повторную попытку.

        Args:
            error: Возникшее исключение.
            attempt: Текущий номер попытки.

        Returns:
            bool: True, если нужна повторная попытка.
        """
        if attempt >= self.config.max_retries:
            return False

        # Проверяем специальные исключения
        if isinstance(error, QuotaExceededError):
            return True  # Всегда повторяем при превышении квоты

        # Проверяем HTTP-коды ошибок
        if isinstance(error, ProxyError) and error.status_code in self.config.retry_codes:
            return True

        # Проверяем типы исключений
        return any(isinstance(error, exc_type) for exc_type in self.config.retry_exceptions)

    async def execute_with_retry(
        self,
        operation: Callable[..., Any],
        *args,
        context: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Any:
        """Выполняет операцию с автоматическими повторными попытками.

        Args:
            operation: Асинхронная функция для выполнения.
            *args: Позиционные аргументы для operation.
            context: Контекстная информация для логирования.
            **kwargs: Именованные аргументы для operation.

        Returns:
            Any: Результат успешного выполнения operation.

        Raises:
            Exception: Если все попытки завершились неудачей.
        """
        context = context or {}
        operation_name = operation.__name__
        start_time = datetime.now()
        last_error = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                if attempt > 1:
                    delay = self._calculate_delay(attempt - 1)
                    self._logger.log_info(
                        f"Попытка {attempt}/{self.config.max_retries} для {operation_name}. "
                        f"Ожидание {delay:.2f} сек...",
                        extra={**context, "attempt": attempt, "delay": delay}
                    )
                    await asyncio.sleep(delay)

                result = await operation(*args, **kwargs)

                if attempt > 1:
                    duration = (datetime.now() - start_time).total_seconds()
                    self._logger.log_info(
                        f"Успешное выполнение {operation_name} после {attempt} попыток.",
                        extra={**context, "attempts": attempt, "duration": duration}
                    )
                return result

            except Exception as e:
                last_error = e
                duration = (datetime.now() - start_time).total_seconds()

                if self.should_retry(e, attempt):
                    self._logger.log_warning(
                        f"Попытка {attempt}/{self.config.max_retries} для {operation_name} "
                        f"завершилась ошибкой: {str(e)}",
                        extra={**context, "attempt": attempt, "duration": duration}
                    )
                    continue

                self._logger.log_error(
                    f"Все попытки ({attempt}) для {operation_name} исчерпаны.",
                    extra={**context, "final_error": str(e), "duration": duration}
                )
                raise

        # Если мы здесь, значит все попытки исчерпаны
        raise last_error