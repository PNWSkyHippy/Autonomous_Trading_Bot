@echo off
title Trading Bot Engine
cd /d "%~dp0"

echo ============================================================
echo   TRADING BOT ENGINE
echo ============================================================
echo Working directory: %CD%
echo.

if not exist "venv312\Scripts\activate.bat" (
  echo ERROR: venv312\Scripts\activate.bat was not found.
  echo.
  goto end
)

call "venv312\Scripts\activate.bat"
set PYTHONIOENCODING=utf-8

echo Starting bot_engine.py...
echo.
python bot_engine.py

echo.
echo bot_engine.py exited with code %ERRORLEVEL%.

:end
echo.
echo This window is intentionally staying open so startup errors are visible.
