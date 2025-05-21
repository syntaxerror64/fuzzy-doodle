# -*- coding: utf-8 -*-
"""Основной модуль прокси-сервера для OpenRouter API."""

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import aiohttp

from utils_proxy_open.request_processor import RequestProcessor
from utils_proxy_open.key_manager import KeyManager
from utils_proxy_open.config_manager import load_config
from utils_proxy_open.background_tasks import AsyncBackgroundTasks
from utils_proxy_open.error_handler import ErrorHandler
from utils_proxy_open import logger_setup

# Инициализация FastAPI
app = FastAPI(
    title="OpenRouter Proxy Server",
    description="Прокси-сервер для OpenRouter API с поддержкой множественных API-ключей",
    version="2.0.0"
)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Type"],
    max_age=3600
)

# Зависимости FastAPI
async def get_request_processor() -> RequestProcessor:
    """Получает экземпляр RequestProcessor из app.state."""
    return app.state.request_processor

async def get_key_manager() -> KeyManager:
    """Получает экземпляр KeyManager из app.state."""
    return app.state.key_manager_instance

async def get_config() -> dict:
    """Получает текущую конфигурацию."""
    return app.state.current_config

async def get_error_handler() -> ErrorHandler:
    """Получает экземпляр ErrorHandler из app.state."""
    return app.state.error_handler

@app.on_event("startup")
async def startup_event():
    """Инициализация при запуске приложения."""
    try:
        # Загрузка конфигурации
        config = load_config()
        app.state.current_config = config

        # Инициализация aiohttp сессии
        app.state.http_session = aiohttp.ClientSession()

        # Инициализация ErrorHandler
        app.state.error_handler = ErrorHandler()

        # Инициализация KeyManager
        app.state.key_manager_instance = KeyManager(
            keys_file_path=config["keys_file"],
            api_url=config["api_url"],
            validation_timeout=config["request_timeout"]
        )

        # Инициализация RequestProcessor
        app.state.request_processor = RequestProcessor(
            key_manager=app.state.key_manager_instance,
            api_url=config["api_url"],
            request_timeout=config["request_timeout"],
            max_retries=config["max_retries"],
            base_delay=config.get("retry_base_delay", 1.0),
            max_delay=config.get("retry_max_delay", 8.0)
        )

        # Инициализация и запуск фоновых задач
        app.state.background_tasks = AsyncBackgroundTasks()
        await app.state.background_tasks.start_background_tasks_async(
            app,
            app.state.http_session
        )

        logger_setup.logger.log_info("Сервер успешно запущен и инициализирован")

    except Exception as e:
        logger_setup.logger.log_critical(f"Ошибка при инициализации сервера: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Очистка ресурсов при остановке приложения."""
    try:
        if hasattr(app.state, "background_tasks"):
            await app.state.background_tasks.stop_background_tasks_async()

        if hasattr(app.state, "http_session"):
            await app.state.http_session.close()

        logger_setup.logger.log_info("Сервер успешно остановлен")
    except Exception as e:
        logger_setup.logger.log_error(f"Ошибка при остановке сервера: {e}")

@app.options("/chat/completions")
@app.options("/v1/chat/completions")
async def chat_completions_options(request: Request):
    """Обработка OPTIONS запросов для эндпоинтов /chat/completions и /v1/chat/completions."""
    # Логируем детали входящего OPTIONS запроса
    logger_setup.logger.log_info(
        "Получен OPTIONS запрос",
        extra={
            "endpoint": request.url.path,
            "headers": dict(request.headers),
            "client": request.client.host,
            "port": request.client.port
        }
    )
    
    # Возвращаем правильные CORS заголовки
    return JSONResponse(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "3600"
        }
    )

@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def proxy_chat_completions(
    request: Request,
    request_processor: RequestProcessor = Depends(get_request_processor),
    error_handler: ErrorHandler = Depends(get_error_handler),
    config: dict = Depends(get_config)
):
    """Обработка запросов к эндпоинтам /chat/completions и /v1/chat/completions."""
    try:
        # Логируем детали входящего POST запроса
        logger_setup.logger.log_info(
            "Получен POST запрос",
            extra={
                "endpoint": request.url.path,
                "headers": dict(request.headers),
                "client": request.client.host,
                "port": request.client.port
            }
        )

        request_data = await request.json()
        # Логируем тело запроса (без конфиденциальных данных)
        safe_request_data = {
            k: v for k, v in request_data.items()
            if k not in ["api_key", "authorization", "key"]
        }
        logger_setup.logger.log_info(
            "Данные запроса",
            extra={"request_data": safe_request_data}
        )

        return await request_processor.process_request(
            session=app.state.http_session,
            request_data=request_data,
            fallback_models=config.get("fallback_models") if config.get("enable_fallback") else None
        )
    except Exception as e:
        logger_setup.logger.log_error(
            "Ошибка при обработке запроса",
            extra={
                "endpoint": request.url.path,
                "error": str(e),
                "client": request.client.host
            }
        )
        return await error_handler.handle_error(e, {"endpoint": request.url.path})

@app.post("/reload")
async def reload_config(
    key_manager: KeyManager = Depends(get_key_manager),
    error_handler: ErrorHandler = Depends(get_error_handler)
):
    """Ручная перезагрузка конфигурации и ключей."""
    try:
        # Перезагрузка конфигурации
        app.state.current_config = load_config()

        # Перезагрузка ключей
        await key_manager.load_keys(
            validate_on_load=True,
            session=app.state.http_session
        )

        return JSONResponse(content={
            "status": "reloaded",
            "keys_count": await key_manager.get_available_keys_count()
        })
    except Exception as e:
        return await error_handler.handle_error(e, {"endpoint": "/reload"})

if __name__ == "__main__":
    config = load_config()
    uvicorn.run(
        "proxy:app",
        host=config["host"],
        port=config["port"],
        reload=True
    )