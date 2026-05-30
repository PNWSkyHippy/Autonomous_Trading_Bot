@echo off
title HTML Trading Dashboard Bridge
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0StartWebDashboard.ps1"
