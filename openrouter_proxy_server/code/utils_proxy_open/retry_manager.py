# -*- coding: utf-8 -*-
"""Модуль для управления повторными попытками запросов с экспоненциальной задержкой."""

import asyncio
import random
from typing import TypeVar, Callable, Optional, Any, Dict
from functools import wraps

from . import logger_setup

# Тип для обобщенного результата функции
T = TypeVar('T')

class RetryConfig:
    """Конфигурация для механизма повторных попыток."""
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

class RetryManager:
    """Менеджер повторных попыток с экспоненциальной задержкой."""
    
    def __init__(self, config: RetryConfig):
        self.config = config
        self._logger = logger_setup.logger
    
    def calculate_delay(self, attempt: int) -> float:
        """Вычисляет задержку для текущей попытки с экспоненциальным ростом.
        
        Args:
            attempt: Номер текущей попытки (начиная с 1).
            
        Returns:
            float: Время задержки в секундах.
        """
        delay = self.config.base_delay * (self.config.exponential_base ** (attempt - 1))
        delay = min(delay, self.config.max_delay)
        
        if self.config.jitter:
            # Добавляем случайное отклонение ±15%
            jitter_range = delay * 0.15
            delay += random.uniform(-jitter_range, jitter_range)
        
        return max(0, delay)  # Гарантируем неотрицательную задержку

    async def execute_with_retry(
        self,
        operation: Callable[..., Any],
        *args,
        retry_on_exceptions: tuple = (Exception,),
        context: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Any:
        """Выполняет операцию с автоматическими повторными попытками.
        
        Args:
            operation: Асинхронная функция для выполнения.
            *args: Позиционные аргументы для operation.
            retry_on_exceptions: Кортеж исключений, при которых выполнять повторные попытки.
            context: Словарь с контекстной информацией для логирования.
            **kwargs: Именованные аргументы для operation.
            
        Returns:
            Any: Результат успешного выполнения operation.
            
        Raises:
            Exception: Если все попытки завершились неудачей.
        """
        context = context or {}
        operation_name = operation.__name__
        last_exception = None
        
        for attempt in range(1, self.config.max_retries + 1):
            try:
                if attempt > 1:
                    delay = self.calculate_delay(attempt - 1)
                    self._logger.log_info(
                        f"Попытка {attempt}/{self.config.max_retries} для {operation_name}. "
                        f"Ожидание {delay:.2f} сек...",
                        extra=context
                    )
                    await asyncio.sleep(delay)
                
                result = await operation(*args, **kwargs)
                if attempt > 1:
                    self._logger.log_info(
                        f"Успешное выполнение {operation_name} после {attempt} попыток.",
                        extra=context
                    )
                return result
                
            except retry_on_exceptions as e:
                last_exception = e
                self._logger.log_warning(
                    f"Попытка {attempt}/{self.config.max_retries} для {operation_name} "
                    f"завершилась ошибкой: {str(e)}",
                    extra=context
                )
                
                # Если это последняя попытка, пробрасываем исключение
                if attempt == self.config.max_retries:
                    self._logger.log_error(
                        f"Все попытки ({self.config.max_retries}) для {operation_name} исчерпаны.",
                        extra=context
                    )
                    raise last_exception

def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retry_on_exceptions: tuple = (Exception,)
) -> Callable:
    """Декоратор для добавления механизма повторных попыток к асинхронным функциям.
    
    Args:
        max_retries: Максимальное количество попыток.
        base_delay: Базовая задержка между попытками (в секундах).
        max_delay: Максимальная задержка между попытками (в секундах).
        exponential_base: Основание для экспоненциального роста задержки.
        jitter: Добавлять ли случайное отклонение к задержке.
        retry_on_exceptions: Кортеж исключений, при которых выполнять повторные попытки.
        
    Returns:
        Callable: Декорированная функция с механизмом повторных попыток.
    """
    config = RetryConfig(
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        exponential_base=exponential_base,
        jitter=jitter
    )
    retry_manager = RetryManager(config)
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            return await retry_manager.execute_with_retry(
                func,
                *args,
                retry_on_exceptions=retry_on_exceptions,
                **kwargs
            )
        return wrapper
    return decorator