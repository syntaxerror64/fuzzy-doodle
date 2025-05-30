# OpenRouter Proxy Server

Этот проект представляет собой асинхронный прокси-сервер на FastAPI для API OpenRouter. Он позволяет распределять запросы между несколькими API-ключами OpenRouter, обрабатывать ошибки, управлять ключами и вести логирование.

## Основные возможности

*   **Проксирование запросов**: Перенаправляет запросы `/v1/chat/completions` на API OpenRouter.
*   **Управление ключами**:
    *   Загружает ключи из файла `keys.csv`.
    *   Автоматически выбирает рабочий ключ для каждого запроса.
    *   Помечает ключи как 'bad' при возникновении ошибок (4xx, 5xx, таймауты) и сохраняет статус в `keys.csv`.
    *   Поддерживает временную блокировку ключей при ошибке 429 (Too Many Requests) с последующей попыткой восстановления.
    *   Позволяет добавлять/удалять/валидировать ключи через меню.
    *   **Механизм пометки ключей**:
        *   Статус ключа ('good'/'bad'), причина ошибки и время пометки 'bad' хранятся в памяти.
        *   **Немедленная пометка 'bad'**: При ошибках 401 (Unauthorized), 5xx (Internal Server Error), таймаутах, ошибках соединения или неожиданных ошибках ключ сразу помечается как 'bad', и статус сохраняется в `keys.csv`.
        *   **Отложенная пометка 'bad' (402/429)**: При ошибках 402 (Payment Required) или 429 (Too Many Requests), ключ временно запоминается как неудачный для текущего запроса. Только *после* успешного выполнения запроса с *другим* ключом, все ранее неудачные ключи (из-за 402/429) помечаются как 'bad', и их статус вместе со статусом успешного ключа сохраняется в `keys.csv`. Время пометки 'bad' также записывается в файл.
        *   **Пометка 'good'**: После успешного запроса или восстановления ключ помечается как 'good', и статус сохраняется (при отложенной пометке 'bad' - вместе с ними).
*   **Обработка ошибок и ретраи**: Пытается повторить запрос с другим ключом при возникновении ошибок 401, 402, 404, 429 и 5xx. Для 500 ошибки сначала пытается повторить с тем же ключом.
*   **Поддержка Streaming**: Корректно обрабатывает потоковые ответы от API, включая обнаружение ошибок 429/5xx внутри потока и механизм ретрая с буферизацией.
*   **Фоновые задачи**: Периодически проверяет и пытается восстановить 'bad' ключи (учитывая `recovery_timestamp` для 429).
*   **Конфигурация**: Настройки сервера хранятся в `config.ini`.
*   **Логирование**: Подробное логирование событий в `proxy.log` и в консоль.
*   **Интерактивное меню**: Позволяет управлять сервером и ключами через консольное меню.
*   **Синхронизация с Google Sheets**: Опционально загружает и синхронизирует ключи из Google Sheet.
*   **Эндпоинт `/v1/models`**: Возвращает список доступных бесплатных моделей OpenRouter.

## Структура проекта

*   `main.py`: Точка входа, запуск FastAPI и фоновых задач.
*   `proxy.py`: (Вероятно, устарел или используется для синхронной версии)
*   `keys.csv`: Файл для хранения API-ключей OpenRouter (формат: `key;status;usage_count_or_error_msg;marked_bad_at`).
*   `config.ini`: Файл конфигурации.
*   `proxy.log`: Файл логов.
*   `requirements.txt`: Зависимости Python.
*   `Readme.md`: Этот файл.
*   `CHANGELOG.md`: История изменений.
*   `utils_proxy_open/`: Пакет с основной логикой:
    *   `__init__.py`: Инициализация пакета.
    *   `config_manager.py`: Управление конфигурацией (`config.ini`).
    *   `key_manager.py`: Управление API-ключами (`keys.csv`).
    *   `proxy_handler.py`: Обработка HTTP-запросов FastAPI.
    *   `logger_setup.py`: Настройка логирования.
    *   `menu.py`: Реализация интерактивного меню.
    *   `background_tasks.py`: Реализация фоновых задач (восстановление ключей, сохранение).

## Установка и запуск

1.  **Клонируйте репозиторий:**
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```
2.  **Создайте и активируйте виртуальное окружение (рекомендуется):**
    ```bash
    python -m venv venv
    # Windows
    .\venv\Scripts\activate
    # macOS/Linux
    source venv/bin/activate
    ```
3.  **Установите зависимости:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Настройте `config.ini`:**
    *   Укажите `api_url`, `request_timeout`, `max_retries` и другие параметры.
    *   При необходимости настройте секцию `[GoogleSheetSync]`.
5.  **Добавьте ключи в `keys.csv`:**
    *   Создайте файл `keys.csv`, если его нет.
    *   Добавьте ключи в формате `your-api-key;good;0;` (каждый ключ на новой строке, последнее поле - время пометки 'bad', оставляйте пустым для 'good' ключей).
6.  **Запустите сервер:**
    ```bash
    py main.py
    ```
    Сервер будет доступен по адресу, указанному в `config.ini` (по умолчанию `http://127.0.0.1:8000`). В консоли также запустится интерактивное меню.

## Использование

Отправляйте запросы к API OpenRouter через ваш локальный прокси-сервер:

*   **Chat Completions**: `http://127.0.0.1:8000/v1/chat/completions`
*   **List Models**: `http://127.0.0.1:8000/v1/models`

Используйте любой HTTP-клиент или библиотеку (например, `requests` в Python, `curl`, Postman). Прокси автоматически выберет рабочий ключ и обработает запрос.
