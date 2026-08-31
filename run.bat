@echo off
cd /d "%~dp0"
title Movie Bot
call venv\Scripts\activate.bat
python bot.py
echo.
echo ===== Bot has stopped (window stays open). Read any error above. =====
cmd /k
