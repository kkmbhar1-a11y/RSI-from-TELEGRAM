@echo off
REM Wrapper script for Windows Task Scheduler.
REM Adjust the paths below to match your installation.

cd /d "C:\stock_alert_system"
"C:\Python312\python.exe" telegram_listener.py >> logs\scheduler_stdout.log 2>&1
