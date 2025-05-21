# utils_proxy_open/proxy_handler.py
# -*- coding: utf-8 -*-
"""Модуль для обработки FastAPI-запросов к прокси."""

import asyncio
import json
import time
import sys
import logging
from typing import Dict, Any, Optional, Union, AsyncGenerator, List, Tuple # Added List, Tuple

import aiohttp
from fastapi import FastAPI, Request, Response, APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse

# Импорты из проекта (потребуют адаптации к async)
from . import key_manager
from . import config_manager
from . import logger_setup
from . import background_tasks # Потребует значительной адаптации


# --- Кастомные исключения ---
class QuotaExceededInStreamError(Exception):
    """Исключение, генерируемое при обнаружении ошибки 429 (квота) внутри потока."""
    pass

class InternalApiErrorInStream(Exception):
    """Исключение, генерируемое при обнаружении ошибки 500 (или другой не 429) внутри потока."""
    pass

class InternalProxyError(Exception):
    """Внутренняя ошибка прокси для корректной обработки состояний."""
    pass

# --- Зависимости FastAPI ---
# Зависимости теперь будут получать объекты из app.state

async def get_http_session(request: Request) -> aiohttp.ClientSession:
    """Зависимость для получения aiohttp сессии из app.state."""
    if not hasattr(request.app.state, 'http_session') or request.app.state.http_session is None:
        logger_setup.logger.log_critical("aiohttp.ClientSession не найдена в app.state!")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера: HTTP сессия не готова.")
    return request.app.state.http_session

async def get_key_manager_instance(request: Request) -> key_manager.KeyManager:
    """Зависимость для получения экземпляра KeyManager из app.state."""
    if not hasattr(request.app.state, 'key_manager_instance'):
        logger_setup.logger.log_critical("Экземпляр KeyManager не найден в app.state!")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера: KeyManager не готов.")
    return request.app.state.key_manager_instance

async def get_config_manager(request: Request) -> Any: # Возвращаем модуль, тип Any
    """Зависимость для получения модуля config_manager из app.state."""
    if not hasattr(request.app.state, 'config_manager'):
        logger_setup.logger.log_critical("Модуль config_manager не найден в app.state!")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера: ConfigManager не готов.")
    return request.app.state.config_manager

async def get_background_tasks_runner(request: Request) -> background_tasks.AsyncBackgroundTasks:
    """Зависимость для получения экземпляра AsyncBackgroundTasks из app.state."""
    if not hasattr(request.app.state, 'background_tasks_runner'):
        logger_setup.logger.log_critical("AsyncBackgroundTasks runner не найден в app.state!")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера: BackgroundTasks runner не готов.")
    return request.app.state.background_tasks_runner

async def get_request_data(request: Request) -> Dict[str, Any]:
    """Зависимость для получения и валидации JSON из тела запроса."""
    try:
        data = await request.json()
        if not data:
            raise HTTPException(status_code=400, detail="Пустое тело запроса (ожидался JSON)")
        return data
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Некорректный JSON в теле запроса")
    except Exception as e:
        logger_setup.logger.log_exception(f"Ошибка при чтении тела запроса: {repr(e)}")
        raise HTTPException(status_code=400, detail=f"Ошибка чтения запроса: {repr(e)}")

# --- APIRouter ---
proxy_router = APIRouter()

# --- NEW ENDPOINT: /v1/models ---
@proxy_router.get("/models",
                  summary="List Available Models (Filtered for Free)",
                  description="Retrieves the list of available models from OpenRouter and filters them to show only free models (ID ending with ':free').",
                  response_model=Dict[str, Any]) # Define a basic response model
async def get_models(
    request: Request,
    session: aiohttp.ClientSession = Depends(get_http_session),
    # Optional: Add config dependency if needed for timeout etc.
    # config: Any = Depends(get_config_manager)
):
    """
    Handles GET requests to /v1/models. Fetches models from OpenRouter,
    filters for free ones, and returns the list.
    """
    openrouter_models_url = "https://openrouter.ai/api/v1/models"
    request_id = f"models-{int(time.time()*1000)}" # Simple request ID
    logger = logger_setup.logger # Get the logger instance

    logger.log_info(f"Request [{request_id}]: Received request for /v1/models")
    start_time = time.time()

    try:
        # Optional: Get timeout from config if needed
        # timeout_sec = config.get("request_timeout", 30)
        timeout = aiohttp.ClientTimeout(total=30) # Use a default timeout for now

        async with session.get(openrouter_models_url, timeout=timeout) as response:
            duration = time.time() - start_time
            if response.status == 200:
                try:
                    data = await response.json()
                    if not isinstance(data, dict) or "data" not in data or not isinstance(data["data"], list):
                        logger.log_error(f"Request [{request_id}]: Invalid JSON structure received from {openrouter_models_url}. Status: {response.status}", duration=duration)
                        raise HTTPException(status_code=502, detail="Invalid response structure from upstream API")

                    # Filter for free models
                    free_models = [model for model in data["data"] if isinstance(model, dict) and model.get("id", "").endswith(":free")]

                    filtered_response = {"data": free_models}
                    logger.log_info(f"Request [{request_id}]: Successfully fetched and filtered models. Found {len(free_models)} free models. Duration: {duration:.3f}s")
                    return JSONResponse(content=filtered_response)

                except json.JSONDecodeError:
                    logger.log_error(f"Request [{request_id}]: Failed to decode JSON from {openrouter_models_url}. Status: {response.status}", duration=duration)
                    raise HTTPException(status_code=502, detail="Failed to decode upstream API response")
                except Exception as e:
                    logger.log_exception(f"Request [{request_id}]: Error processing models response: {repr(e)}")
                    raise HTTPException(status_code=500, detail=f"Internal error processing models: {repr(e)}")
            else:
                # Handle non-200 responses from OpenRouter
                error_body = await response.text()
                logger.log_error(f"Request [{request_id}]: Upstream API ({openrouter_models_url}) returned status {response.status}. Body: {error_body[:200]}...", duration=duration)
                raise HTTPException(status_code=502, detail=f"Upstream API error (Status: {response.status})")

    except aiohttp.ClientConnectionError as e:
        duration = time.time() - start_time
        logger.log_error(f"Request [{request_id}]: Network error connecting to {openrouter_models_url}: {repr(e)}", duration=duration)
        raise HTTPException(status_code=502, detail=f"Network error connecting to upstream API: {repr(e)}")
    except asyncio.TimeoutError:
        duration = time.time() - start_time
        logger.log_error(f"Request [{request_id}]: Timeout connecting to {openrouter_models_url}", duration=duration)
        raise HTTPException(status_code=504, detail="Upstream API timeout")
    except Exception as e:
        duration = time.time() - start_time
        logger.log_exception(f"Request [{request_id}]: Unexpected error handling /v1/models: {repr(e)}")
        # Ensure duration is logged even in unexpected errors
        logger.log_error(f"Request [{request_id}]: Unexpected error during /v1/models handling.", duration=duration)
        raise HTTPException(status_code=500, detail=f"Internal server error: {repr(e)}")

# --- Stream Generator (Raises Exceptions on In-Stream Errors) ---
async def _stream_response_generator(response: aiohttp.ClientResponse, request_id: str, api_key: str) -> AsyncGenerator[bytes, None]:
    """
    Генератор для потоковой передачи ответа API.
    Анализирует чанки на предмет ошибки 429/500 и генерирует исключения.
    Гарантированно закрывает response в блоке finally.
    """
    buffer = b""
    try:
        async for chunk in response.content.iter_any():
            raw_chunk_str = chunk.decode('utf-8', errors='replace')
            logger_setup.logger.log_debug(f"Request [{request_id}] Stream Chunk Raw: {raw_chunk_str}")
            buffer += chunk
            buffer_str = buffer.decode('utf-8', errors='replace')

            # Проверка на ошибки внутри потока
            if 'data: {' in buffer_str and '"error":' in buffer_str:
                data_prefix = "data: "
                start_index = buffer_str.find(data_prefix)
                if start_index != -1:
                    # Пытаемся извлечь JSON ошибки
                    json_str_match = buffer_str[start_index + len(data_prefix):].strip()
                    brace_level = 0
                    end_json_index = -1
                    in_string = False
                    # Простой парсер для поиска закрывающей скобки JSON
                    for i, char in enumerate(json_str_match):
                        if char == '"' and (i == 0 or json_str_match[i-1] != '\\'): in_string = not in_string
                        elif not in_string:
                            if char == '{': brace_level += 1
                            elif char == '}':
                                brace_level -= 1
                                if brace_level == 0: end_json_index = i; break
                    # Если нашли полный JSON
                    if end_json_index != -1:
                        potential_json = json_str_match[:end_json_index+1]
                        try:
                            error_data = json.loads(potential_json)
                            if isinstance(error_data, dict) and "error" in error_data:
                                error_info = error_data["error"]
                                if isinstance(error_info, dict):
                                    error_code = error_info.get("code")
                                    error_message = error_info.get("message", "Unknown error in stream")

                                    # Обработка ошибки 429 (Quota) -> Raise Exception
                                    if error_code == 429:
                                        logger_setup.logger.log_warning(f"Request [{request_id}]: Обнаружена ошибка 429 (Quota Exceeded) ВНУТРИ ПОТОКА для ключа {api_key[:8]}...: {error_message}")
                                        raise QuotaExceededInStreamError(error_message) # RAISE

                                    # Обработка ошибки 404 (Not Found) -> Raise Exception
                                    elif error_code == 404:
                                        logger_setup.logger.log_warning(f"Request [{request_id}]: Обнаружена ошибка 404 (Not Found) ВНУТРИ ПОТОКА для ключа {api_key[:8]}...: {error_message}")
                                        raise InternalApiErrorInStream(error_message) # Используем InternalApiErrorInStream для единообразия обработки не-429 ошибок в потоке

                                    # Обработка ошибки 500 (Internal API Error) -> Raise Exception
                                    elif error_code >= 500 and error_code < 600:
                                         logger_setup.logger.log_warning(f"Request [{request_id}]: Обнаружена ошибка {error_code} (Internal API Error) ВНУТРИ ПОТОКА для ключа {api_key[:8]}...: {error_message}")
                                         raise InternalApiErrorInStream(error_message) # RAISE

                            # Удаляем обработанную часть из буфера
                            buffer = buffer[start_index + len(data_prefix) + end_json_index + 1:]
                        except json.JSONDecodeError: pass # Ошибка парсинга JSON, игнорируем и ждем больше данных
                        except QuotaExceededInStreamError: raise # Re-raise specific exceptions
                        except InternalApiErrorInStream: raise # Re-raise specific exceptions
                        except Exception as parse_err:
                            logger_setup.logger.log_warning(f"Request [{request_id}]: Ошибка обработки JSON из буфера стрима: {parse_err}. Буфер: {buffer_str[:200]}...")

            # Ограничиваем размер буфера, чтобы избежать переполнения памяти
            if len(buffer) > 8192: buffer = buffer[-4096:]

            # Отправляем чанк дальше, если не было ошибки
            try:
                chunk_text = chunk.decode('utf-8', errors='replace').strip()
                if chunk_text:
                    # Логируем, что чанк идет в буфер (или к клиенту, если буферизация не используется)
                    logger_setup.logger.log_debug(f"Request [{request_id}] [Yielding Chunk]{chunk_text}[/Yielding Chunk]")
            except Exception as log_ex:
                logger_setup.logger.log_warning(f"Request [{request_id}] Error logging yield chunk: {log_ex}")
            yield chunk

    except asyncio.CancelledError:
        logger_setup.logger.log_warning(f"Request [{request_id}]: Потоковая передача отменена.")
        raise # Передаем исключение дальше
    except Exception as stream_ex:
        # Логируем неожиданные ошибки генератора
        if not isinstance(stream_ex, (QuotaExceededInStreamError, InternalApiErrorInStream)):
             logger_setup.logger.log_exception(f"Request [{request_id}]: Неожиданная ошибка в генераторе потока: {repr(stream_ex)}")
        raise # Передаем исключение дальше (включая QuotaExceeded/InternalApiError)
    finally:
        # Гарантированно закрываем соединение с API
        if not response.closed:
            await response.release()
            logger_setup.logger.log_debug(f"Request [{request_id}]: aiohttp response released in _stream_response_generator finally block.")


# --- MAIN ENDPOINT: /v1/chat/completions ---
@proxy_router.post("/chat/completions",
                   summary="Proxy Chat Completions to OpenRouter",
                   description="Proxies chat completion requests to the OpenRouter API, handling key rotation, retries, and streaming.",
                   tags=["Proxy"])
async def handle_proxy_chat_completions(
    request: Request,
    background_tasks_fastapi: BackgroundTasks, # FastAPI's BackgroundTasks for things like saving keys *after* response
    session: aiohttp.ClientSession = Depends(get_http_session),
    km_instance: key_manager.KeyManager = Depends(get_key_manager_instance),
    request_data: Dict[str, Any] = Depends(get_request_data) # Use dependency to get validated JSON
):
    """
    Handles POST requests to /v1/chat/completions.
    Selects an API key, forwards the request to OpenRouter, handles retries,
    and streams the response if requested. Implements internal buffering for streams
    to retry transparently on mid-stream errors.
    """
    start_time = time.time()
    request_id = f"chat-{int(start_time*1000)}"
    logger = logger_setup.logger
    logger.log_info(f"Request [{request_id}]: Received request for /chat/completions. Model: {request_data.get('model', 'N/A')}, Stream: {request_data.get('stream', False)}")

    try:
        params = await _get_request_params(request, request_data)
    except Exception as e:
        logger.log_exception(f"Request [{request_id}]: Error getting request parameters.")
        return create_error_json_response(f"Internal error getting parameters: {repr(e)}", 500, request_id=request_id)

    api_key = None
    response_result = None
    retries = 0
    max_retries = params["max_retries"]
    last_error_response = None # Store the last actual error response from API
    # Список для хранения ключей, которые не сработали с 402/429 в рамках этого запроса
    keys_failed_4xx_this_request: List[Tuple[str, str, Optional[int]]] = []

    while retries <= max_retries:
        if retries > 0:
            logger.log_info(f"Request [{request_id}]: Retry {retries}/{max_retries}...")
            await asyncio.sleep(0.2 * retries) # Exponential backoff (simple)

        api_key = None # Reset api_key for each retry attempt
        api_response = None # Reset api_response

        try:
            # Get a key for this attempt
            api_key = await km_instance.get_random_key()
            if not api_key:
                logger.log_error(f"Request [{request_id}]: No available API keys found after {retries} retries.")
                if last_error_response and isinstance(last_error_response, Response):
                     return last_error_response
                return create_error_json_response("No available API keys", 503, model=params["requested_model"], duration=(time.time() - start_time), request_id=request_id)

            logger.log_debug(f"Request [{request_id}]: Attempting with key {api_key[:8]}... (Retry {retries})")

            # Perform the API call
            api_response = await _perform_api_call(
                session, params["api_url"], api_key, request_data, params["request_timeout_sec"], request_id
            )

            # Process the initial response (can return Response, None, "STREAM_READY", or "RETRY_500_SAME_KEY")
            response_result = await _process_api_response(
                api_response, api_key, km_instance, params, start_time, request_id
            )

            # --- Handle different outcomes from _process_api_response ---

            if isinstance(response_result, Response):
                # Non-streaming success or unrecoverable API error.
                # Если это успех (2xx), нужно пометить ключи из keys_failed_4xx_this_request как 'bad'
                if 200 <= response_result.status_code < 300:
                    await _mark_failed_keys_and_save(km_instance, keys_failed_4xx_this_request, request_id, logger)
                logger.log_debug(f"Request [{request_id}]: Returning final non-stream response (status {response_result.status_code}) to client.")
                return response_result # Exit the loop and function

            elif response_result == "STREAM_READY":
                # Initial API call successful for streaming. Attempt to buffer the stream.
                # _handle_buffered_stream now returns StreamingResponse on success,
                # or a tuple (key, error_message, recovery_delay) on 4xx stream error,
                # or None on other stream errors needing retry.
                stream_result = await _handle_buffered_stream(
                    api_response, api_key, km_instance, request_id, logger, params, start_time
                )

                if isinstance(stream_result, StreamingResponse):
                    # Buffering and processing successful. Mark failed keys and return stream.
                    await _mark_failed_keys_and_save(km_instance, keys_failed_4xx_this_request, request_id, logger)
                    return stream_result # Exit loop and function
                elif isinstance(stream_result, tuple):
                    # 4xx error occurred during buffering. Add to list and retry.
                    failed_key, err_msg, rec_delay = stream_result
                    keys_failed_4xx_this_request.append((failed_key, err_msg, rec_delay))
                    logger.log_info(f"Request [{request_id}]: Added key {failed_key[:8]} to delayed 'bad' list due to stream error. Retrying...")
                    retries += 1
                    continue # Go to the next iteration
                else: # stream_result is None (other stream error)
                    # An error occurred during buffering (logged inside handler), need to retry
                    # Key status (non-4xx) might have been updated inside _handle_buffered_stream
                    retries += 1
                    continue # Go to the next iteration

            elif response_result == "RETRY_500_SAME_KEY":
                 # Specific case: Retry the *same* key on initial 500 error
                 logger.log_warning(f"Request [{request_id}]: Retrying same key {api_key[:8]}... due to 500 error.")
                 # Let the loop continue. Key status not changed yet.
                 retries += 1
                 continue # Go to next iteration

            elif isinstance(response_result, tuple) and response_result[0] == "retry_4xx":
                # Recoverable 402/429 error from initial call. Add to list and try next key.
                _, failed_key, err_msg, rec_delay = response_result
                # Проверяем, что ключ еще не в списке (на случай странных повторных ошибок)
                if not any(k[0] == failed_key for k in keys_failed_4xx_this_request):
                    keys_failed_4xx_this_request.append((failed_key, err_msg, rec_delay))
                    logger.log_info(f"Request [{request_id}]: Added key {failed_key[:8]} to delayed 'bad' list due to {err_msg[:10]}. Retrying...")
                else:
                    logger.log_warning(f"Request [{request_id}]: Key {failed_key[:8]} already in delayed 'bad' list. Retrying...")
                retries += 1
                continue # Go to next iteration

            elif response_result is None:
                # Other recoverable error from initial call (e.g., timeout, connection error), try next key
                # Key status (timeout, 401, 50x) was updated in _process_api_response
                logger.log_info(f"Request [{request_id}]: Recoverable error (non-4xx) with key {api_key[:8]}... Trying next key.")
                retries += 1
                continue # Go to next iteration
            else:
                 # Should not happen
                 logger.log_error(f"Request [{request_id}]: Unexpected result from _process_api_response: {response_result}")
                 retries += 1
                 continue # Go to next iteration

        # Removed specific handling for QuotaExceededInStreamError and InternalApiErrorInStream here,
        # as they are now handled during the buffering phase above for streaming requests.
        except InternalProxyError as e:
             # Handle specific internal errors if needed
             logger.log_error(f"Request [{request_id}]: Internal Proxy Error: {e}")
             # Decide if retry is appropriate
             retries += 1
             continue
        except Exception as e:
            # Catch unexpected errors during the retry loop (e.g., getting key, initial API call)
            logger.log_exception(f"Request [{request_id}]: Unhandled exception in retry loop (key {api_key[:8] if api_key else 'N/A'}).")
            # Mark the key as potentially bad if we had one and it wasn't a key retrieval issue
            # Only mark immediately for non-4xx related unhandled exceptions
            if api_key and not any(api_key == k[0] for k in keys_failed_4xx_this_request):
                 await km_instance.update_key_status(key=api_key, is_good=False, error_message=f"Unhandled Exception: {repr(e)}", save_now=True) # Save immediately for safety
            # Ensure api_response is released if it exists and is open
            if isinstance(api_response, aiohttp.ClientResponse) and not api_response.closed:
                await api_response.release()
            retries += 1
            # Store this error in case it's the last one
            last_error_response = create_error_json_response(f"Unhandled internal error: {repr(e)}", 500, key=(api_key or "unknown"), model=params["requested_model"], duration=(time.time() - start_time), request_id=request_id)
            continue # Try next retry

    # --- Max retries exceeded ---
    duration = time.time() - start_time
    logger.log_error(f"Request [{request_id}]: Max retries ({max_retries}) exceeded. Failing request.")
    if last_error_response and isinstance(last_error_response, Response):
        return last_error_response
    else:
        # Generic error if no specific API error was captured
        return create_error_json_response(f"Failed after {max_retries} retries. No keys available or persistent errors.", 503, model=params["requested_model"], duration=duration, request_id=request_id)


# --- Вспомогательные функции ---

async def _handle_buffered_stream(
    api_response: aiohttp.ClientResponse,
    api_key: str,
    km_instance: key_manager.KeyManager,
    request_id: str,
    logger: logger_setup.LoggerSetup,
    params: Dict[str, Any],
    start_time: float
) -> Optional[Union[StreamingResponse, Tuple[str, str, int], None]]:
    """
    Handles buffering the stream response, detecting in-stream errors.
    Returns:
        - StreamingResponse on success.
        - Tuple (key, error_message, recovery_delay) for 4xx errors in stream (for delayed marking).
        - None for other errors requiring a generic retry (key status updated internally if needed).
    """
    logger.log_debug(f"Request [{request_id}]: Starting internal buffering for key {api_key[:8]}...")
    buffered_chunks: List[bytes] = []
    stream_error_occurred = False
    stream_error_details = ""
    stream_error_type = None # To distinguish 4xx from others
    return_value = None # Value to return from this function

    try:
        # Iterate through the generator, buffering chunks
        async for chunk in _stream_response_generator(api_response, request_id, api_key):
            buffered_chunks.append(chunk)
        # If loop completes without error, stream was fully buffered successfully
        logger.log_info(f"Request [{request_id}]: Stream fully buffered successfully ({len(buffered_chunks)} chunks) with key {api_key[:8]}.")

    except (QuotaExceededInStreamError, InternalApiErrorInStream) as stream_err:
        stream_error_occurred = True
        stream_error_details = repr(stream_err)
        stream_error_type = type(stream_err)
        logger.log_warning(f"Request [{request_id}]: Error detected during stream buffering for key {api_key[:8]}...: {stream_error_details}. Signaling retry.")
        error_msg = f"Error in stream: {stream_error_details}"
        if isinstance(stream_err, QuotaExceededInStreamError):
            # Signal 4xx error back to main loop for delayed marking
            recovery = 86400 # 24 hours for 429
            return_value = (api_key, f"429 in stream: {stream_error_details}", recovery)
        else: # InternalApiErrorInStream
            # Mark 5xx in stream bad immediately
            await km_instance.update_key_status(key=api_key, is_good=False, error_message=error_msg, save_now=True, recovery_delay_sec=3600)
            return_value = None # Signal generic retry

    except Exception as unexpected_err:
        stream_error_occurred = True
        stream_error_details = repr(unexpected_err)
        stream_error_type = type(unexpected_err)
        logger.log_exception(f"Request [{request_id}]: Unexpected error during stream buffering for key {api_key[:8]}... Signaling retry.")
        # Mark key as bad immediately for unexpected errors
        await km_instance.update_key_status(key=api_key, is_good=False, error_message=f"Unexpected buffering error: {stream_error_details}", save_now=True)
        return_value = None # Signal generic retry

    # --- After attempting to buffer ---
    if stream_error_occurred:
        return return_value # Return tuple for 4xx, None for others
    else:
        # Stream buffered successfully, prepare StreamingResponse
        logger.log_debug(f"Request [{request_id}]: Preparing buffered stream response for client.")
        # Define a simple generator to yield buffered chunks
        async def stream_buffered_response(chunks: list[bytes]):
            for chunk in chunks:
                yield chunk
                await asyncio.sleep(0) # Allow context switching if needed

        # Mark the successful key as good (but don't save yet)
        await km_instance.update_key_status(key=api_key, is_good=True, error_message=None, save_now=False, increment_count=True, recovery_delay_sec=None)
        logger.log_info(f"Request [{request_id}] completed successfully (stream)", key=api_key, model=params["requested_model"], status=200, duration=(time.time() - start_time))

        return StreamingResponse(stream_buffered_response(buffered_chunks), media_type="text/event-stream")


async def _mark_failed_keys_and_save(
    km_instance: key_manager.KeyManager,
    failed_keys_info: List[Tuple[str, str, Optional[int]]],
    request_id: str,
    logger: logger_setup.LoggerSetup
):
    """Marks keys that failed with 4xx during the request as 'bad' and saves all changes."""
    if not failed_keys_info:
        # If no keys failed with 4xx, just save potential changes to the successful key
        await km_instance.save_keys()
        return

    logger.log_info(f"Request [{request_id}]: Marking {len(failed_keys_info)} keys as 'bad' after successful request completion...")
    keys_marked = []
    for key, error_msg, recovery_delay in failed_keys_info:
        await km_instance.update_key_status(
            key=key,
            is_good=False,
            error_message=error_msg,
            save_now=False, # Don't save individually
            recovery_delay_sec=recovery_delay
        )
        keys_marked.append(key[:8])
    logger.log_info(f"Request [{request_id}]: Marked keys {keys_marked} as 'bad'.")
    await km_instance.save_keys() # Save all changes together


def create_error_json_response(
    message: str,
    status_code: int,
    key: str = "unknown",
    model: str = "systems",
    duration: float = 0.0,
    request_id: Optional[str] = None
) -> JSONResponse:
    """Создает стандартизированный JSON-ответ об ошибке FastAPI и логирует ее."""
    error_details = { "error": { "message": message, "type": "proxy_error", "code": status_code, "request_id": request_id } }
    log_message = f"[{request_id or 'NO_ID'}] {message}"
    log_func = logger_setup.logger.log_error if status_code >= 500 else logger_setup.logger.log_warning
    log_func(f"API {'Error' if status_code >= 500 else 'Warning'}: {log_message}", key=key, model=model, status_code=status_code, duration=duration)
    return JSONResponse(content=error_details, status_code=status_code)

async def _get_request_params(request: Request, request_data: Dict[str, Any]) -> Dict[str, Any]:
    """Извлекает параметры запроса и конфигурации."""
    settings = request.app.state.settings if hasattr(request.app.state, 'settings') else {}
    cm_module = request.app.state.config_manager
    return {
        "api_url": settings.get("api_url", cm_module.DEFAULT_SETTINGS["api_url"]),
        "request_timeout_sec": settings.get("request_timeout", cm_module.DEFAULT_SETTINGS["request_timeout"]),
        "max_retries": settings.get("max_retries", cm_module.DEFAULT_SETTINGS["max_retries"]),
        "enable_fallback": settings.get("enable_fallback", cm_module.DEFAULT_SETTINGS["enable_fallback"]),
        "fallback_models": settings.get("fallback_models", cm_module.DEFAULT_SETTINGS["fallback_models"]),
        "requested_model": request_data.get("model", "unknown"),
        "is_streaming": request_data.get("stream", False),
    }

async def _perform_api_call(
    session: aiohttp.ClientSession, api_url: str, api_key: str,
    request_data: Dict[str, Any], timeout_sec: int, request_id: str
) -> Union[aiohttp.ClientResponse, str]:
    """Выполняет один вызов API и обрабатывает ошибки соединения/таймаута."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    logger_setup.logger.log_debug(f"Request [{request_id}]: Вызов API {api_url} с ключом {api_key[:8]}...")
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        # Ensure stream=True is passed for streaming requests to potentially avoid server buffering
        # However, OpenRouter might ignore this hint. The crucial part is handling the response stream correctly.
        response = await session.post(api_url, headers=headers, json=request_data, timeout=timeout)
        return response
    except aiohttp.ClientConnectionError as e:
        logger_setup.logger.log_error(f"Request [{request_id}]: Ошибка сети (Connection Error) при запросе к API с ключом {api_key[:8]}...: {repr(e)}")
        return "connection_error"
    except asyncio.TimeoutError:
        logger_setup.logger.log_error(f"Request [{request_id}]: Таймаут ({timeout_sec}s) при запросе к API с ключом {api_key[:8]}...")
        return "timeout"
    except Exception as e:
        logger_setup.logger.log_exception(f"Request [{request_id}]: Неожиданная ошибка при вызове API с ключом {api_key[:8]}...:")
        return "unexpected_error" # Возвращаем маркер для обработки выше

async def _process_api_response(
    response: Union[aiohttp.ClientResponse, str], api_key: str, km_instance: key_manager.KeyManager,
    params: Dict[str, Any], start_time: float, request_id: str
) -> Optional[Union[Response, str, Tuple[str, str, str, Optional[int]]]]:
    """
    Обрабатывает ответ от API или маркер ошибки.
    Возвращает:
        - Response: Для клиента (успех non-stream или неисправимая ошибка).
        - None: Для ретрая (таймаут, ошибка соединения, 502, 503, 504, 401, unexpected_error). Статус ключа обновляется немедленно.
        - "STREAM_READY": Для начала буферизации потока.
        - "RETRY_500_SAME_KEY": Для ретрая с тем же ключом при 500.
        - Tuple ("retry_4xx", key, error_message, recovery_delay): Для ретрая при 402/429 (статус не обновляется).
    НЕ ЗАКРЫВАЕТ response, если возвращает "STREAM_READY".
    """
    duration = time.time() - start_time

    # Обработка маркеров ошибок от _perform_api_call
    if response == "connection_error": await asyncio.sleep(0.5); return None
    if response == "timeout":
        # Таймаут - помечаем ключ плохим немедленно
        await km_instance.update_key_status(key=api_key, is_good=False, error_message=f"Timeout ({params['request_timeout_sec']}s)", save_now=True)
        logger_setup.logger.log_info(f"Request [{request_id}]: Ключ {api_key[:8]}... помечен как 'bad' из-за таймаута.")
        await asyncio.sleep(0.1); return None
    if response == "unexpected_error":
        # Неожиданная ошибка при вызове API - помечаем плохим немедленно
        await km_instance.update_key_status(key=api_key, is_good=False, error_message="Unexpected API call error", save_now=True)
        logger_setup.logger.log_warning(f"Request [{request_id}]: Ключ {api_key[:8]}... помечен как 'bad' из-за неожиданной ошибки вызова API.")
        return None # Сигнал для ретрая

    if not isinstance(response, aiohttp.ClientResponse):
        logger_setup.logger.log_error(f"Request [{request_id}]: _process_api_response получил неожиданный тип: {type(response)}")
        return create_error_json_response("Внутренняя ошибка сервера: неверный тип ответа API", 500, key=api_key, model=params["requested_model"], duration=duration, request_id=request_id)

    # --- Обработка HTTP ошибок от API (>= 400) ---
    if response.status >= 400:
        error_message = f"Unknown error (status {response.status})"
        error_body = b""
        try:
            error_body = await response.read()
            try: error_data = json.loads(error_body); error_message = error_data.get("error", {}).get("message", error_body.decode('utf-8', errors='ignore'))
            except json.JSONDecodeError: error_message = error_body.decode('utf-8', errors='ignore') or f"Non-JSON error body (status {response.status})"
        except Exception as read_err: error_message = f"Не удалось прочитать тело ошибки: {repr(read_err)}"
        finally:
            # Закрываем тело ответа при ошибке, т.к. стримить его не будем
            if not response.closed: await response.release()

        log_level = logging.ERROR if response.status in [401, 402, 429, 500, 502, 503, 504] else logging.WARNING
        log_message_http_error = f"Request [{request_id}]: Ошибка HTTP {response.status} от API с ключом {api_key[:8]}... ({error_message[:100]})"
        if log_level == logging.ERROR:
            logger_setup.logger.log_error(log_message_http_error, key=api_key, model=params["requested_model"], status=response.status, duration=duration)
        else:
            logger_setup.logger.log_warning(log_message_http_error, key=api_key, model=params["requested_model"], status=response.status, duration=duration)

        # --- Логика обработки ошибок ---
        if response.status == 500:
            return "RETRY_500_SAME_KEY" # Сигнал для ретрая с тем же ключом
        elif response.status in [402, 429]:
            # Ошибки 402/429 - сигнал для ретрая с отложенной пометкой
            recovery_delay = 86400 if response.status == 429 else None # 24ч для 429, нет для 402
            return ("retry_4xx", api_key, f"{response.status}: {error_message}", recovery_delay)
        elif response.status in [401, 502, 503, 504]:
            # Ошибки, требующие немедленной пометки 'bad' и ретрая
            await km_instance.update_key_status(key=api_key, is_good=False, error_message=f"{response.status}: {error_message}", save_now=True)
            logger_setup.logger.log_info(f"Request [{request_id}]: Ключ {api_key[:8]}... помечен как 'bad' из-за {response.status}.")
            await asyncio.sleep(0.1)
            return None # Сигнал для ретрая с новым ключом
        else:
            # Необрабатываемая ошибка API (например, 400, 403, 404), возвращаем клиенту
            logger_setup.logger.log_warning(f"Request [{request_id}] failed with unhandled API error {response.status}", key=api_key, model=params["requested_model"], status=response.status, duration=duration)
            media_type = response.headers.get('Content-Type')
            return Response(content=error_body, status_code=response.status, media_type=media_type)

    # --- Обработка успешного ответа (2xx) ---
    if params["is_streaming"]:
        logger_setup.logger.log_debug(f"Request [{request_id}]: Успешный начальный ответ ({response.status}) для стриминга от API за {duration:.3f} сек.")
        return "STREAM_READY" # Сигнал для начала буферизации
    else:
        # Обработка не-потокового ответа
        try:
            response_body = await response.read()
            if not response.closed: await response.release() # Закрываем тело после чтения

            logger_setup.logger.log_debug(f"Request [{request_id}] Full Response Body: {response_body.decode('utf-8', errors='replace')}")
            response_json = json.loads(response_body)
            used_model = response_json.get("model", params["requested_model"])
            logger_setup.logger.log_debug(f"Request [{request_id}]: Успешный JSON ответ ({response.status}) от API за {duration:.3f} сек. Использована модель: {used_model}")

            # Обновляем статус ключа как успешный (не сохраняем здесь, сохранение после пометки неудавшихся)
            await km_instance.update_key_status(key=api_key, is_good=True, error_message=None, save_now=False, increment_count=True, recovery_delay_sec=None)

            # Проверка Fallback (если нужно)
            if params["enable_fallback"] and used_model != params["requested_model"]:
                if used_model not in params["fallback_models"]:
                    logger_setup.logger.log_warning(f"Request [{request_id}]: Модель '{used_model}' не входит в список разрешенных fallback моделей ({params['fallback_models']}). Отклонено.")
                    # Не помечаем ключ как 'bad', т.к. он рабочий, просто модель не та
                    return create_error_json_response(f"Модель '{used_model}', использованная API, не входит в список разрешенных fallback моделей.", 400, key=api_key, model=params["requested_model"], duration=duration, request_id=request_id)
                else:
                    logger_setup.logger.log_info(f"Request [{request_id}]: Использована fallback модель '{used_model}' вместо запрошенной '{params['requested_model']}'.")

            logger_setup.logger.log_info(f"Request [{request_id}] completed successfully (non-stream)", key=api_key, model=params["requested_model"], status=response.status, duration=duration)
            media_type = response.headers.get('Content-Type', 'application/json')
            # Возвращаем успешный ответ, сохранение статусов произойдет выше
            return Response(content=response_body, status_code=response.status, media_type=media_type)

        except (json.JSONDecodeError, aiohttp.ContentTypeError) as json_err:
            logger_setup.logger.log_error(f"Request [{request_id}]: API вернуло статус {response.status}, но тело не является валидным JSON: {json_err}", key=api_key, model=params["requested_model"], status=response.status, duration=duration)
            if not response.closed: await response.release()
            # Помечаем ключ как хороший (не сохраняем здесь), т.к. API ответило 2xx
            await km_instance.update_key_status(key=api_key, is_good=True, error_message=None, save_now=False, increment_count=True, recovery_delay_sec=None)
            # Возвращаем ошибку клиенту, сохранение статусов произойдет выше
            return create_error_json_response(f"API вернуло некорректный JSON ответ (статус {response.status})", 502, key=api_key, model=params["requested_model"], duration=duration, request_id=request_id)
        except Exception as proc_err:
            logger_setup.logger.log_exception(f"Request [{request_id}]: Ошибка при обработке успешного не-потокового ответа API: {proc_err}")
            if not response.closed: await response.release()
            # Помечаем ключ как хороший (не сохраняем здесь), т.к. API ответило 2xx
            await km_instance.update_key_status(key=api_key, is_good=True, error_message=None, save_now=False, increment_count=True, recovery_delay_sec=None)
            # Возвращаем ошибку клиенту, сохранение статусов произойдет выше
            return create_error_json_response(f"Внутренняя ошибка сервера при обработке ответа API: {repr(proc_err)}", 500, key=api_key, model=params["requested_model"], duration=duration, request_id=request_id)

# --- Функция для подключения роутера к приложению FastAPI ---
def setup_proxy_routes(app: FastAPI):
    """Подключает маршруты прокси к основному приложению FastAPI."""
    app.include_router(proxy_router, prefix="/v1", tags=["Proxy"]) # Добавляем префикс /v1
    logger_setup.logger.log_info("Маршруты прокси (/v1/chat/completions и /v1/models) подключены.")
