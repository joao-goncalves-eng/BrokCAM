@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\criar_atalho.ps1"
pause
