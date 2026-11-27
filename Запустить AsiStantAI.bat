@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AsiStantAI

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python не найден. Установите Python 3.12 с python.org,
  echo отметьте «Add python.exe to PATH» и запустите файл снова.
  echo.
  pause
  exit /b 1
)

python -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo Ставлю зависимости — это нужно один раз.
  python -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo Не удалось поставить зависимости. Проверьте интернет и запустите снова.
    pause
    exit /b 1
  )
)

echo.
echo Запускаю AsiStantAI: http://127.0.0.1:8000
echo Браузер откроется сам через несколько секунд.
echo Чтобы остановить программу, закройте это окно.
echo.

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000/'"
python -m app.cli serve
pause
