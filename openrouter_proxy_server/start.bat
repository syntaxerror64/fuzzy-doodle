@echo off
setlocal enabledelayedexpansion

cd code || (
    echo Ошибка: Директория 'code' не найдена
    pause
    exit /b 1
)

:: Активация виртуального окружения
if exist ..\venv\Scripts\activate.bat (
    call ..\venv\Scripts\activate.bat || (
        echo Ошибка: Не удалось активировать виртуальное окружение
        pause
        exit /b 1
    )
) else (
    echo Виртуальное окружение не найдено. Создание нового окружения...
    py -m venv ..\venv || (
        echo Ошибка: Не удалось создать виртуальное окружение
        pause
        exit /b 1
    )
    call ..\venv\Scripts\activate.bat || (
        echo Ошибка: Не удалось активировать новое виртуальное окружение
        pause
        exit /b 1
    )
    py -m pip install -r requirements.txt || (
        echo Ошибка: Не удалось установить зависимости
        pause
        exit /b 1
    )
)

:: Запуск сервера
py main.py

pause