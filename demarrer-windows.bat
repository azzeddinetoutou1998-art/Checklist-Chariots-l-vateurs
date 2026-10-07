@echo off
REM Changez le mot de passe ci-dessous avant la mise en service
set ADMIN_PASSWORD=ChangezMoi2026
set PORT=8080
cd /d "%~dp0"
python server.py
pause
