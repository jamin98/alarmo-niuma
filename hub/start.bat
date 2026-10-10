@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: 在 Windows 上直接跑 hub（接 Alarmo 或只开网页模拟器）。第一次先: pip install -r requirements.txt
python hub.py %*
pause
