# -*- coding: utf-8 -*-
"""Модуль для обработки HTTP-запросов к OpenRouter API с поддержкой повторных попыток."""

import asyncio
import json
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, AsyncGenerator

import aiohttp
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from . import logger_setup
from .retry_manager import RetryManager, RetryConfig
from .key_manager import KeyManager

class RequestHandler:
    """Обработчик HTTP-запросов к OpenRouter API."""
    
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
        self.retry_config = RetryConfig(
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay
        )
        self.retry_manager = RetryManager(self.retry_config)
        self._logger = logger_setup.logger

    async def _make_request(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        request_data: Dict[str, Any],
        is_streaming: bool
    ) -> Tuple[Any, int]:
        """Выполняет HTTP-запрос к API с заданным ключом.

        Args:
            session: Сессия aiohttp.
            api_key: API-ключ для запроса.
            request_data: Данные запроса.
            is_streaming: Флаг потокового режима.

        Returns:
            Tuple[Any, int]: Результат запроса и код состояния.

        Raises:
            HTTPException: При ошибках запроса.
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        try:
            async with session.post(
                self.api_url,
                headers=headers,
                json=request_data,
                timeout=self.timeout,
                chunked=is_streaming
            ) as response:
                response.raise_for_status()
                if is_streaming:
                    return response, response.status
                else:
                    data = await response.json()
                    return data, response.status

        except aiohttp.ClientResponseError as e:
            # Помечаем ключ как нерабочий для определенных ошибок
            if e.status in (401, 402, 429) or (e.status >= 500 and e.status < 600):
                await self.key_manager.mark_key_as_bad(api_key, str(e))
                self._logger.log_warning(
                    f"Ключ {api_key} помечен как нерабочий из-за ошибки {e.status}",
                    extra={
                        "key": api_key,
                        "status": e.status,
                        "detail": str(e)
                    }
                )
                # Для этих ошибок мы хотим попробовать другой ключ, поэтому re-raise HTTPException
                raise HTTPException(status_code=e.status, detail=str(e))
            # Для других ошибок просто re-raise HTTPException
            raise HTTPException(status_code=e.status, detail=str(e))
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Request timeout")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def _stream_response(
        self,
        response: aiohttp.ClientResponse
    ) -> AsyncGenerator[bytes, None]:
        """Генератор для потоковой передачи ответа.

        Args:
            response: Ответ от API.

        Yields:
            bytes: Чанки данных ответа.
        """
        try:
            async for chunk in response.content.iter_chunks():
                if chunk:
                    yield chunk[0] + b"\n"
        except Exception as e:
            self._logger.log_error(f"Ошибка при потоковой передаче: {e}")
            raise

    async def handle_request(
        self,
        session: aiohttp.ClientSession,
        request_data: Dict[str, Any],
        fallback_models: Optional[list] = None
    ) -> Any:
        """Обрабатывает запрос к API с поддержкой повторных попыток и ротации ключей.

        Args:
            session: Сессия aiohttp.
            request_data: Данные запроса.
            fallback_models: Список разрешенных моделей для fallback.

        Returns:
            Any: Ответ от API (StreamingResponse или JSONResponse).
        """
        start_time = datetime.now()
        requested_model = request_data.get("model", "unknown")
        is_streaming = request_data.get("stream", False)

        while True:
            api_key = await self.key_manager.get_random_key()
            if not api_key:
                duration = (datetime.now() - start_time).total_seconds()
                self._logger.log_error(
                    "Нет доступных ключей",
                    extra={"duration": duration, "model": requested_model}
                )
                raise HTTPException(
                    status_code=503,
                    detail="No valid keys available"
                )

            try:
                result, status_code = await self.retry_manager.execute_with_retry(
                    self._make_request,
                    session=session,
                    api_key=api_key,
                    request_data=request_data,
                    is_streaming=is_streaming,
                    context={"api_key": api_key, "model": requested_model}
                )

                duration = (datetime.now() - start_time).total_seconds()
                await self.key_manager.increment_key_usage(api_key)

                if is_streaming:
                    self._logger.log_info(
                        "Успешный потоковый запрос",
                        extra={
                            "duration": duration,
                            "model": requested_model,
                            "key": api_key,
                            "status": status_code
                        }
                    )
                    return StreamingResponse(
                        self._stream_response(result),
                        media_type="text/event-stream"
                    )
                else:
                    used_model = result.get("model", requested_model)
                    if fallback_models and used_model != requested_model:
                        if used_model not in fallback_models:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Model {used_model} not in fallback list"
                            )

                    self._logger.log_info(
                        "Успешный запрос",
                        extra={
                            "duration": duration,
                            "model": requested_model,
                            "key": api_key,
                            "status": status_code
                        }
                    )
                    return JSONResponse(
                        content=result,
                        status_code=status_code
                    )

            except HTTPException as e:
                # Если ошибка связана с ключом (401, 402, 429, 5xx), пробуем следующий ключ
                if e.status_code in (401, 402, 429) or (e.status_code >= 500 and e.status_code < 600):
                    self._logger.log_warning(
                        f"Попытка с другим ключом из-за ошибки {e.status_code}",
                        extra={
                            "key": api_key,
                            "status": e.status_code,
                            "detail": str(e)
                        }
                    )
                    continue
                # Для других ошибок re-raise
                raise
            except Exception as e:
                duration = (datetime.now() - start_time).total_seconds()
                self._logger.log_error(
                    f"Ошибка при обработке запроса: {e}",
                    extra={
                        "duration": duration,
                        "model": requested_model,
                        "key": api_key
                    }
                )
                raise HTTPException(status_code=500, detail=str(e))