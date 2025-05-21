# -*- coding: utf-8 -*-
"""Модуль для централизованной обработки ошибок в прокси-сервере."""

from typing import Optional, Dict, Any, Type, Union
from fastapi import HTTPException
from fastapi.responses import JSONResponse
import aiohttp
import asyncio

from . import logger_setup

class ProxyError(Exception):
    """Базовый класс для всех ошибок прокси-сервера."""
    def __init__(self, message: str, status_code: int = 500, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}

class APIKeyError(ProxyError):
    """Ошибки, связанные с API-ключами."""
    pass

class QuotaExceededError(APIKeyError):
    """Превышение квоты для API-ключа."""
    def __init__(self, message: str, key_id: str):
        super().__init__(message, status_code=429, details={"key_id": key_id})

class NoValidKeysError(APIKeyError):
    """Отсутствие доступных API-ключей."""
    def __init__(self):
        super().__init__("Нет доступных API-ключей", status_code=503)

class RequestError(ProxyError):
    """Ошибки при выполнении запросов."""
    pass

class TimeoutError(RequestError):
    """Превышение времени ожидания запроса."""
    def __init__(self, timeout: float):
        super().__init__(
            f"Превышено время ожидания запроса ({timeout} сек)",
            status_code=504,
            details={"timeout": timeout}
        )

class UpstreamAPIError(RequestError):
    """Ошибки от вышестоящего API."""
    def __init__(self, status_code: int, message: str, response_data: Optional[Dict[str, Any]] = None):
        super().__init__(
            message,
            status_code=status_code,
            details={"response_data": response_data} if response_data else {}
        )

class ErrorHandler:
    """Централизованный обработчик ошибок для прокси-сервера."""

    def __init__(self):
        self._logger = logger_setup.logger

    async def handle_error(
        self,
        error: Exception,
        context: Optional[Dict[str, Any]] = None
    ) -> JSONResponse:
        """Обрабатывает исключение и возвращает соответствующий JSONResponse.

        Args:
            error: Исключение для обработки.
            context: Дополнительный контекст для логирования.

        Returns:
            JSONResponse с информацией об ошибке.
        """
        context = context or {}
        error_info = self._prepare_error_info(error)

        # Логируем ошибку с контекстом
        log_message = f"{error_info['error_type']}: {error_info['message']}"
        if error_info['status_code'] >= 500:
            self._logger.log_error(log_message, extra=context)
        else:
            self._logger.log_warning(log_message, extra=context)

        return JSONResponse(
            content={
                "error": {
                    "message": error_info['message'],
                    "type": error_info['error_type'],
                    "details": error_info['details']
                }
            },
            status_code=error_info['status_code']
        )

    def _prepare_error_info(self, error: Exception) -> Dict[str, Any]:
        """Подготавливает информацию об ошибке для ответа.

        Args:
            error: Исключение для обработки.

        Returns:
            Словарь с информацией об ошибке.
        """
        if isinstance(error, ProxyError):
            return {
                "message": str(error),
                "status_code": error.status_code,
                "error_type": error.__class__.__name__,
                "details": error.details
            }

        # Обработка стандартных исключений
        if isinstance(error, HTTPException):
            return {
                "message": error.detail,
                "status_code": error.status_code,
                "error_type": "HTTPException",
                "details": {}
            }

        if isinstance(error, aiohttp.ClientError):
            return {
                "message": "Ошибка сетевого взаимодействия",
                "status_code": 502,
                "error_type": error.__class__.__name__,
                "details": {"original_error": str(error)}
            }

        if isinstance(error, asyncio.TimeoutError):
            return {
                "message": "Превышено время ожидания",
                "status_code": 504,
                "error_type": "TimeoutError",
                "details": {}
            }

        # Общий случай для необработанных исключений
        return {
            "message": "Внутренняя ошибка сервера",
            "status_code": 500,
            "error_type": error.__class__.__name__,
            "details": {"original_error": str(error)}
        }