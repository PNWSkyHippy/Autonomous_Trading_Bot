@echo off
title Autonomous Trading Bot

echo ============================================================
echo   AUTONOMOUS DAY TRADING AI - STARTING UP
echo ============================================================
echo.

:: Wait 60 seconds after boot for network to fully connect
echo Waiting for network...
timeout /t 60 /nobreak

:: Navigate to bot folder
cd /d C:\Users\Linda\trading_bot_v2

:: Activate virtual environment
call venv312\Scripts\activate.bat

:: Set UTF-8 encoding for Windows console
set PYTHONIOENCODING=utf-8
chcp 65001 > nul

:: Start the dashboard in a separate window
echo Starting dashboard...
start "Trading Dashboard" cmd /k "cd /d C:\Users\Linda\trading_bot_v2 && venv312\Scripts\activate.bat && set PYTHONIOENCODING=utf-8 && streamlit run dashboard.py"

:: Wait a moment for dashboard to initialize
timeout /t 5 /nobreak

:: Start the main bot engine in this window
echo Starting bot engine...
python bot_engine.py --verbose

:: If bot crashes, wait 30 seconds and restart automatically
:restart
echo.
echo Bot stopped - restarting in 30 seconds...
timeout /t 30 /nobreak
python bot_engine.py
goto restart
