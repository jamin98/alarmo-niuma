@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: 安装 VB-CABLE 虚拟声卡（按住牛马说话要用），详见 install-vbcable.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-vbcable.ps1" %*
pause
