@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bot-control.ps1" stop
timeout /t 3 >nul
