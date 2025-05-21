# -*- coding: utf-8 -*-
"""Модуль для обработки HTTP-запросов к OpenRouter API с улучшенной логикой повторных попыток."""

import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, AsyncGenerator, Union

import aiohttp
from fastapi.responses import StreamingResponse, JSONResponse

from . import logger_setup
from .error_handler import (
    ErrorHandler,
    ProxyError,
    QuotaExceededError,
    NoValidKeysError,
    TimeoutError,
    UpstreamAPIError
)
from .retry_strategy import RetryStrategy, RetryStrategyConfig
from .key_manager import KeyManager

class RequestProcessor:
    """Процессор HTTP-запросов к OpenRouter API с поддержкой повторных попыток и ротации ключей."""

    def __init__(
        self,
        key_manager: KeyManager,
        api_url: str,
        request_timeout: int = 30,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 8.0
    ):
        self.key_manager = key_manager
        self.api_url = api_url
        self.timeout = aiohttp.ClientTimeout(total=request_timeout)
        
        # Инициализация стратегии повторных попыток
        self.retry_strategy = RetryStrategy(
            RetryStrategyConfig(
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay
            )
        )
        
        self.error_handler = ErrorHandler()
        self._logger = logger_setup.logger

    async def _prepare_request(
        self,
        api_key: str,
        request_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Подготавливает данные для запроса.

        Args:
            api_key: API-ключ для запроса.
            request_data: Исходные данные запроса.

        Returns:
            Dict[str, Any]: Подготовленные данные запроса.
        """
        return {
            "headers": {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            "json": request_data
        }

    async def _make_request(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        request_data: Dict[str, Any],
        is_streaming: bool
    ) -> Tuple[Any, int]:
        """Выполняет HTTP-запрос к API.

        Args:
            session: Сессия aiohttp.
            api_key: API-ключ для запроса.
            request_data: Данные запроса.
            is_streaming: Флаг потокового режима.

        Returns:
            Tuple[Any, int]: Результат запроса и код состояния.

        Raises:
            ProxyError: При различных ошибках запроса.
        """
        request_params = await self._prepare_request(api_key, request_data)

        try:
            async with session.post(
                self.api_url,
                headers=request_params["headers"],
                json=request_params["json"],
                timeout=self.timeout,
                chunked=is_streaming
            ) as response:
                if response.status >= 400:
                    error_data = await response.json()
                    raise UpstreamAPIError(
                        status_code=response.status,
                        message=error_data.get("error", {}).get("message", str(error_data)),
                        response_data=error_data
                    )

                if is_streaming:
                    return response, response.status
                else:
                    data = await response.json()
                    return data, response.status

        except asyncio.TimeoutError:
            raise TimeoutError(self.timeout.total)
        except aiohttp.ClientError as e:
            raise UpstreamAPIError(
                status_code=getattr(e, "status", 502),
                message=str(e)
            )

    async def _stream_response(
        self,
        response: aiohttp.ClientResponse,
        request_id: str
    ) -> AsyncGenerator[bytes, None]:
        """Генератор для потоковой передачи ответа.

        Args:
            response: Ответ от API.
            request_id: Идентификатор запроса.

        Yields:
            bytes: Чанки данных ответа.
        """
        try:
            async for chunk in response.content.iter_chunks():
                if chunk:
                    yield chunk[0] + b"\n"
        except Exception as e:
            self._logger.log_error(
                f"Ошибка при потоковой передаче (request_id: {request_id}): {e}"
            )
            raise

    async def process_request(
        self,
        session: aiohttp.ClientSession,
        request_data: Dict[str, Any],
        fallback_models: Optional[list] = None
    ) -> Union[StreamingResponse, JSONResponse]:
        """Обрабатывает запрос к API с поддержкой повторных попыток и ротации ключей.

        Args:
            session: Сессия aiohttp.
            request_data: Данные запроса.
            fallback_models: Список разрешенных моделей для fallback.

        Returns:
            Union[StreamingResponse, JSONResponse]: Ответ от API.

        Raises:
            NoValidKeysError: Если нет доступных ключей.
            ProxyError: При других ошибках обработки запроса.
        """
        start_time = datetime.now()
        request_id = f"req-{int(start_time.timestamp()*1000)}"
        requested_model = request_data.get("model", "unknown")
        is_streaming = request_data.get("stream", False)

        context = {
            "request_id": request_id,
            "model": requested_model,
            "is_streaming": is_streaming
        }

        while True:
            api_key = await self.key_manager.get_random_key()
            if not api_key:
                duration = (datetime.now() - start_time).total_seconds()
                self._logger.log_error(
                    "Нет доступных ключей",
                    extra={**context, "duration": duration}
                )
                raise NoValidKeysError()

            context["api_key"] = api_key

            try:
                result, status_code = await self.retry_strategy.execute_with_retry(
                    self._make_request,
                    session=session,
                    api_key=api_key,
                    request_data=request_data,
                    is_streaming=is_streaming,
                    context=context
                )

                duration = (datetime.now() - start_time).total_seconds()
                await self.key_manager.increment_key_usage(api_key)

                if is_streaming:
                    self._logger.log_info(
                        "Успешный потоковый запрос",
                        extra={**context, "duration": duration, "status": status_code}
                    )
                    return StreamingResponse(
                        self._stream_response(result, request_id),
                        media_type="text/event-stream"
                    )
                else:
                    used_model = result.get("model", requested_model)
                    if fallback_models and used_model != requested_model:
                        if used_model not in fallback_models:
                            raise UpstreamAPIError(
                                status_code=400,
                                message=f"Model {used_model} not in fallback list"
                            )

                    self._logger.log_info(
                        "Успешный запрос",
                        extra={**context, "duration": duration, "status": status_code}
                    )
                    return JSONResponse(
                        content=result,
                        status_code=status_code
                    )

            except QuotaExceededError:
                # Пробуем следующий ключ
                continue
            except Exception as e:
                # Для остальных ошибок возвращаем ответ через error_handler
                return await self.error_handler.handle_error(e, context)